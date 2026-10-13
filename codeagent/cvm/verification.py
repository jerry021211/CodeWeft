"""Conservative adapters; command success alone is never test success."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
import re
import shlex

from codeagent.cvm.snapshot import digest


def simple_check(command):
    if not isinstance(command, str) or re.search(r'[;&|<>`$()\r\n]', command):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if not args:
        return None
    if args[0] in {'python', 'python3', 'python.exe'} and args[1:3] in (['-m', 'unittest'], ['-m', 'pytest']):
        runner, rest = args[2], args[3:]
    elif args[0] == 'pytest':
        runner, rest = 'pytest', args[1:]
    else:
        return None
    # Do not support injection options, alternate runners or collection-only invocations.
    flags = {'-v', '-vv', '-q', '--verbose', '--quiet'}
    values = {'-s', '-p', '-t'} if runner == 'unittest' else {'-k', '-m'}
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in values:
            index += 1
            if index >= len(rest) or rest[index].startswith('-'):
                return None
        elif arg.startswith('-') and arg not in flags:
            return None
        elif not re.fullmatch(r'[\w./\\:*? =()!\-]+', arg):
            return None
        index += 1
    return {'runner': runner, 'argv': args, 'scope': rest}


@dataclass(frozen=True, slots=True)
class VerificationDefinition:
    """Trusted host/user declaration; deliberately absent from model tool schemas."""
    command: str
    inputs: tuple[str, ...]
    cwd: str = '.'
    source_ref: str = 'host'
    source_kind: str = 'user'
    task_id: str = ''

    def __post_init__(self):
        host_check = self.source_kind == 'host' and re.fullmatch(r'host:[a-z_]+:[a-z_]+', self.command)
        if ((not simple_check(self.command) and not host_check) or not self.inputs or isinstance(self.inputs, str)
                or any(not isinstance(p, str) or not p.strip() for p in self.inputs)
                or any(any(mark in p for mark in '*?[]') for p in self.inputs)
                or self.source_kind not in {'user', 'approved_plan', 'project_config', 'host'}
                or not self.source_ref or not isinstance(self.cwd, str)):
            raise ValueError('Invalid trusted verification definition')
        object.__setattr__(self, 'inputs', tuple(self.inputs))

    @property
    def id(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class TrustedVerificationAdapter:
    """Host-only binding to a registered handler, never selected by tool output text."""
    definitions: tuple[VerificationDefinition, ...]
    select: Callable[[dict], str]
    read_result: Callable

    def __post_init__(self):
        if not self.definitions or any(d.source_kind != 'host' for d in self.definitions):
            raise ValueError('Structured checks require trusted host definitions')


def test_result(output, runner, *, executed=True, cancelled=False):
    """Read original process metadata plus a complete terminal summary."""
    if cancelled or output.outcome == 'cancelled':
        return 'cancelled', {}
    if output.process_running:
        return 'running', {}
    if not executed or output.status == 'blocked' or output.exit_code in {126, 127, 9009} or output.outcome in {'infrastructure_error', 'permission_denied'}:
        return 'blocked', {}
    if output.outcome == 'timeout':
        return 'timeout', {}
    if output.process_id is None or output.exit_code is None:
        return 'unknown', {}
    if not output.source_complete or output.storage_error:
        return 'unknown', {}
    archive = getattr(output, 'archive', None)
    text = archive.tail if archive is not None and archive.finished else str(output)
    # A projected page without the original archive cannot prove a terminal summary.
    if archive is None and (output.has_more or output.truncated_reason):
        return 'unknown', {}
    if runner == 'unittest':
        match = re.search(r'(?m)^Ran (\d+) tests? in [^\n]+\n\s*\n?(OK(?: \([^\n]*\))?|FAILED \([^\n]*\))\s*$', text.strip())
        if not match:
            return 'unknown', {}
        count = int(match[1])
        skipped = re.search(r'skipped=(\d+)', match[2])
        skipped = int(skipped[1]) if skipped else 0
        expected_failures = re.search(r'expected failures=(\d+)', match[2])
        expected_failures = int(expected_failures[1]) if expected_failures else 0
        summary = {'collected': count, 'skipped': skipped, 'expected_failures': expected_failures}
        if match[2].startswith('FAILED') or output.exit_code != 0:
            return 'failed', summary
        return ('passed' if count > skipped + expected_failures and output.status == 'success' else 'unknown'), summary
    lines = text.strip().splitlines()
    last = lines[-1].strip('= ') if lines else ''
    if not re.fullmatch(r'(?:\d+ (?:passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)(?:, )?)+ in [\d.]+s(?: \([^\n]+\))?', last):
        return 'unknown', {}
    summary = {key: int(number) for number, key in re.findall(r'(\d+) (passed|failed|skipped|deselected|xfailed|xpassed|errors?)', last)}
    if output.exit_code != 0 or any(summary.get(key) for key in ('failed', 'error', 'errors')):
        return 'failed', summary
    return ('passed' if summary.get('passed', 0) > 0 and output.status == 'success' else 'unknown'), summary
