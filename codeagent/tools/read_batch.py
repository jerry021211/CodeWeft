"""Compose independent source pages without cutting their versioned cursors."""
import json

from codeagent.tools.base import ToolOutput
from codeagent.tools.output_limits import READ_BODY_CHARS, RESULT_METADATA_CHARS
from codeagent.tools.output_pages import allocate


def render_batch(entries, allowance=READ_BODY_CHARS):
    from codeagent.tools.read import resize_read_page

    # The budget is shared, so batching cannot multiply the read output cap.
    demands = [len(getattr(value, 'body', '')) for _, value in entries]
    shares = allocate(demands, max(0, min(READ_BODY_CHARS, allowance)))
    rendered = [(path, resize_read_page(value, share + RESULT_METADATA_CHARS))
                for (path, value), share in zip(entries, shares)]
    failures = sum(value.status != 'success' for _, value in rendered)
    status = 'error' if failures == len(entries) else 'success'
    manifest = dict(file_count=len(entries), failed_files=failures,
                    continuation='Continue each file with read_file(file_path, offset, char_offset, expected_version).')
    sections = ['[read_file batch] ' + json.dumps(manifest, ensure_ascii=False) + '\n']
    for path, value in rendered:
        sections.append('\n[file] ' + json.dumps(path, ensure_ascii=False) + '\n' + str(value) + '\n')
    result = ToolOutput(''.join(sections), status=status,
                        outcome='partial_success' if 0 < failures < len(entries) else '')
    result.body = ''.join(getattr(value, 'body', '') for _, value in rendered)
    result.metadata_chars = len(result) - len(result.body)
    result.page_ready = True
    result.has_more = any(value.has_more for _, value in rendered)
    result.source_complete = all(value.status == 'success' for _, value in rendered)
    result.read_batch = rendered
    result.page_renderer = lambda size: render_batch(entries, size)
    return result
