"""Own a server process tree even when the parent exits before its children."""
import os
import signal
import time


class ProcessOwner:
    def __init__(self, process):
        self.process, self.job = process, None
        self.wait_handles = {}
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
                        ('min_ws', c.c_size_t), ('max_ws', c.c_size_t), ('active_limit', w.DWORD),
                        ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [c.c_void_p, w.LPCWSTR], w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel = kernel
        job = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)) or not kernel.AssignProcessToJobObject(job, w.HANDLE(int(process._handle))):
            if job:
                kernel.CloseHandle(job)
            process.kill()
            process.wait(timeout=2)
            raise OSError('Cannot own language-server process tree')
        self.job = job

    def prepare_close(self):
        """Retain handles before graceful exit removes children from job accounting."""
        if not self.job:
            return
        import ctypes as c
        from ctypes import wintypes as w
        capacity = 64
        while capacity <= 4096:
            class Processes(c.Structure):
                _fields_ = [('assigned', w.DWORD), ('count', w.DWORD), ('ids', c.c_size_t * capacity)]
            info = Processes()
            if self.kernel.QueryInformationJobObject(self.job, 3, c.byref(info), c.sizeof(info), None):
                for pid in info.ids[:info.count]:
                    if pid not in self.wait_handles:
                        handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                        if handle:
                            self.wait_handles[pid] = handle
                return
            if c.get_last_error() != 234:  # ERROR_MORE_DATA
                return
            capacity *= 2

    def close(self):
        if os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        if self.job:
            self.prepare_close()
            import ctypes as c
            from ctypes import wintypes as w
            class Accounting(c.Structure):
                _fields_ = [(name, c.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                    (name, w.DWORD) for name in ('faults', 'total', 'active', 'terminated')]
            self.kernel.TerminateJobObject(self.job, 1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                info = Accounting()
                if not self.kernel.QueryInformationJobObject(self.job, 1, c.byref(info), c.sizeof(info), None) or info.active == 0:
                    break
                time.sleep(.01)
            self.kernel.CloseHandle(self.job)
            self.job = None
            # ActiveProcesses=0 alone can precede final handle / cwd release.
            deadline = time.monotonic() + 2
            for handle in self.wait_handles.values():
                self.kernel.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
                self.kernel.CloseHandle(handle)
            self.wait_handles.clear()
