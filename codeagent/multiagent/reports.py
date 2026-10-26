"""Separate member claims from runtime-observed tool and check evidence."""
import json
import re
import hashlib


REPORT_INSTRUCTIONS = ('Finish with a JSON object (no extra prose): '
    '{"summary":"brief result","changes":["path: what changed"],'
    '"checks":["what you actually ran and its result"],'
    '"limitations":["failures, unverified requirements or needed follow-up"]}. '
    'These are your claims; Runtime records independent check evidence. Do not claim unrun tests passed.\n')


def worker_report(text, stop_reason, evidence):
    raw = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
    try:
        claims = json.loads(fenced[1] if fenced else raw)
    except (ValueError, TypeError):
        claims = None
    structured = isinstance(claims, dict) and isinstance(claims.get('summary'), str)
    if not structured:
        claims = {'summary': text}
    claims = {k: claims[k] for k in ('summary', 'changes', 'checks', 'limitations')
              if k in claims and (isinstance(claims[k], str) if k == 'summary' else
                                 isinstance(claims[k], list) and all(isinstance(v, str) for v in claims[k]))}
    return dict(version=1, claims=claims, structured=structured, stop_reason=stop_reason,
                tool_evidence=list(evidence), verification='not_run')


def tool_evidence(tool, output):
    # Do not duplicate full write_file bodies or shell scripts in every root
    # tool result. Full activity stays in the trace/output archive.
    label = str(tool.input.get('command') or tool.input.get('path') or tool.input.get('file_path') or '')
    return dict(tool=tool.name, label=label[:1000], label_truncated=len(label) > 1000,
                input_hash=hashlib.sha256(json.dumps(tool.input, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                status=getattr(output, 'status', 'unknown'),
                exit_code=getattr(output, 'exit_code', None),
                changed_files=list(getattr(output, 'changed_files', ())),
                output_id=getattr(output, 'output_id', None),
                execution_id=getattr(output, 'execution_id', None))
