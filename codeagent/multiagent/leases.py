"""Project-wide native execution leases, independent of conversation/data paths."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import time

from codeagent.worktrees.errors import WorktreeError
from codeagent.worktrees.snapshots import exclusive_file


@contextmanager
def project_access(workspace, check, on_wait=lambda: None):
    # Outside the project: acquiring a lease must not change the deliverable tree.
    identity = os.path.normcase(str(Path(workspace).resolve()))
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / 'codeagent-native-project-locks' / (key + '.lock')
    notified = False
    while True:
        check()
        lease = exclusive_file(path)
        try:
            lease.__enter__()
            break
        except WorktreeError:
            if not notified:
                on_wait()
                notified = True
            time.sleep(.05)
    try:
        check()
        yield
    finally:
        lease.__exit__(None, None, None)
