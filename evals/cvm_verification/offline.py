"""Two deterministic behaviors to test measurement, never used as live model evidence."""
from types import SimpleNamespace
from evals.context_journey.offline import PARSER, EXPORTER, TOTALS


class OfflineSDK:
    def __init__(self, case):
        self.case, self.messages, self.serial = case, self, 0
        self.queue = []

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass

    def post(self, path, *, body, cast_to):
        return self.create(**body)

    def begin(self, phase):
        self.queue = []
        def read(path='README.md'):
            self.queue.append(('read_file', {'file_path': path}))
        def write(suite, source):
            self.queue.append(('write_file', {'file_path': f'src/{suite}.py', 'content': source}))
        def check(suite):
            self.queue.append(('run_project_tests', {'suite': suite}))
        if phase['id'] == 'initial':
            read()
        elif phase['id'] == 'repair':
            write('parser', PARSER)
            check('parser')
        elif self.case == 'V04':
            pass
        elif self.case == 'V03':
            write('totals', TOTALS)
        elif self.case in {'V05', 'V06'}:
            check('parser')
            write('parser', PARSER + '# changed after check\n')
            read()
        elif self.case == 'V08':
            write('parser', PARSER)
            check('parser')
        else:
            write('exporter', EXPORTER)
            check('exporter')
        self.followed = False

    def create(self, **kwargs):
        self.serial += 1
        if not self.queue and not self.followed and self.case in {'V05', 'V06'} and 'CVM 验证建议' in str(kwargs['messages']):
            self.queue.append(('run_project_tests', {'suite': 'parser'}))
            self.followed = True
        if self.queue:
            name, arguments = self.queue.pop(0)
            content = [{'type': 'tool_use', 'id': f'offline-{self.serial}', 'name': name, 'input': arguments}]
            stop = 'tool_use'
        else:
            content = [{'type': 'text', 'text': '固定响应演练完成；不代表真实模型质量。'}]
            stop = 'end_turn'
        return SimpleNamespace(content=content, stop_reason=stop, usage=None)
