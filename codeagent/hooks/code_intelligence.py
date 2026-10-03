"""Project edit feedback consumed by Agent tool-result projection."""
import json
import time

from codeagent.tools.lsp import LspTool
from codeagent.tools.search_code import SearchCodeTool


class CodeIntelligenceFeedback:
    # Agent copies recreate this hook against their own registry.
    agent_local = True

    def __init__(self, registry):
        self.registry = registry

    def __call__(self, tool_use, output):
        if output.status != 'success' or not output.changed_files:
            return
        owners = self.registry().owners()
        for owner in owners:
            if isinstance(owner, SearchCodeTool):
                owner.service.invalidate(output.changed_files)
        lsp = next((owner.service for owner in owners if isinstance(owner, LspTool)), None)
        if lsp is None:
            return
        deadline = time.monotonic() + lsp.config.feedback_seconds
        reports = []
        for path in dict.fromkeys(output.changed_files):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(reports) >= 5:
                reports.append({'status': 'incomplete', 'reason': 'Edit diagnostic feedback time/file limit reached'})
                break
            report = lsp.execute('diagnostics', path, timeout=remaining)
            if len(report.get('diagnostics', [])) > 3:
                report['diagnostics'] = report['diagnostics'][:3]
                report['truncated'] = True
            for diagnostic in report.get('diagnostics', []):
                if len(diagnostic['message']) > 300:
                    diagnostic['message'] = diagnostic['message'][:300] + '…'
                    report['truncated'] = True
            reports.append(report)
        # Keep execution facts on ToolOutput intact. Agent explicitly projects this
        # field into the next model request, including parallel commit paths.
        output.context_feedback = '\n<edit_diagnostics>\n' + json.dumps(reports, ensure_ascii=False) + '\n</edit_diagnostics>'
