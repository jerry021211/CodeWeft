"""Use the existing cancellable process runner without Candidate state."""
from pathlib import Path

from codeagent.runtime.validation import ValidationRunner, permission_input, command_label, diagnose
from codeagent.permissions import PermissionPolicy


class CheckFailed(RuntimeError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__(evidence["output"][-6000:])


def run_checks(path, checks, token, broker, emit):
    path = Path(path)
    # Native checks only: do not mutate the process-wide environment or ordinary
    # Agent commands. Other build outputs follow the project's ignore/config.
    runner = ValidationRunner(stopped=lambda: token.is_cancelled,
                              environment={'PYTHONDONTWRITEBYTECODE': '1'})
    policy = PermissionPolicy(workspace=path, broker=broker, cancellation=token)
    evidence = []
    for command in checks:
        token.raise_if_cancelled()
        decision = policy.check("bash", permission_input(path, command))
        token.raise_if_cancelled()
        if not decision.allowed:
            item = dict(command=command_label(command), status="blocked", output=decision.reason,
                        category="authorization", duration_ms=0, exit_code=126)
        else:
            status, code, output, duration = runner.run(path, command)
            item = dict(command=command_label(command), status=status, exit_code=code,
                        output=output, duration_ms=duration)
            if status != "passed":
                item["diagnosis"] = diagnose(status, output)
            item["process_unknown"] = runner.validation_cleanup_unknown
        evidence.append(item)
        emit(item)
        token.raise_if_cancelled()
        if item["status"] != "passed":
            raise CheckFailed(item)
    return evidence
