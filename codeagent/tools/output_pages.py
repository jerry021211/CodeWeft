"""Typed result pages. Renderers accept a BODY allowance, never slice envelopes."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from codeagent.tools.base import ToolOutput, normalize_tool_output
from codeagent.tools.output_limits import RESULT_METADATA_CHARS, DEFAULT_BODY_CHARS

FIELDS = ('status', 'exit_code', 'outcome', 'process_id', 'process_running',
          'returned_range', 'next_cursor', 'has_more', 'source_complete', 'output_id',
          'truncated_reason', 'scan_complete', 'storage_error', 'source_version')


def facts(output):
    return {key: deepcopy(getattr(output, key, None)) for key in FIELDS}


def page(source, body, **metadata):
    result = ToolOutput('')
    result.__dict__.update(normalize_tool_output(source).__dict__)
    for key, value in metadata.items():
        setattr(result, key, value)
    required = {**getattr(source, 'result_metadata', {}), **facts(result)}
    required.update(metadata)
    header = '[tool result] ' + json.dumps(required, ensure_ascii=False, separators=(',', ':')) + '\n'
    # Optional diagnostics cannot displace status or recovery information.
    if len(header) > RESULT_METADATA_CHARS:
        required = facts(result)
        if required.get('storage_error'):
            required['storage_error'] = 'storage_error; unsaved content cannot be recovered'
        header = '[tool result] ' + json.dumps(required, ensure_ascii=False, separators=(',', ':')) + '\n'
    if len(header) > RESULT_METADATA_CHARS:
        raise ValueError('Necessary tool metadata exceeds reservation; use an output_id for long paths')
    value = ToolOutput(header + body)
    value.__dict__.update(result.__dict__)
    value.body = body
    value.metadata_chars = len(header)
    value.page_ready = True
    value.result_metadata = required
    return value


def text_page(source, text, allowance=DEFAULT_BODY_CHARS, *, start=0):
    body = text[:allowance]
    if body.endswith('\r') and text[len(body):len(body) + 1] == '\n':
        body = body[:-1]
    breaks = list(re.finditer(r'\r\n|\r|\n', body))
    cursor = {'offset': len(breaks) + 1, 'char_offset': len(body) - (breaks[-1].end() if breaks else 0)}
    result = page(source, body, returned_range={'start': start, 'end': start + len(body)},
                  has_more=len(body) < len(text),
                  next_cursor=cursor if len(body) < len(text) else None,
                  truncated_reason='page_limit' if len(body) < len(text) else getattr(source, 'truncated_reason', None))
    result.page_renderer = lambda size: text_page(source, text, size, start=start)
    return result


def json_page(source, payload, allowance=DEFAULT_BODY_CHARS, *, records_key='results', max_records=None):
    """Keep valid objects, ranked records and precise quote ranges."""
    if isinstance(payload, list):
        # The envelope stays an object, while the structured result retains its
        # array type. The archive contains the original array without wrapping.
        result = json_page(source, {'result': payload}, allowance, records_key='result', max_records=max_records)
        result.original_payload = payload
        result.page_renderer = lambda size: json_page(source, payload, size, max_records=max_records)
        return result
    if records_key not in payload:
        selected = {}
        for key, value in payload.items():
            candidate = {**selected, key: value}
            if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) <= allowance:
                selected = candidate
        body = json.dumps(selected, ensure_ascii=False, separators=(',', ':')) if allowance >= 2 else ''
        more = len(selected) < len(payload)
        result = page(source, body, returned_range={'fields': len(selected)}, has_more=more,
                      next_cursor={'offset': 1, 'char_offset': 0} if more else None,
                      truncated_reason='fields_omitted' if more else None)
        rendered = ToolOutput(json.dumps({**selected, '_output': facts(result)}, ensure_ascii=False, separators=(',', ':')))
        rendered.__dict__.update(result.__dict__)
        rendered.page_renderer = lambda size: json_page(source, payload, size, records_key=records_key, max_records=max_records)
        rendered.original_payload = payload
        rendered.metadata_chars = len(rendered) - len(body)
        return rendered
    value = deepcopy(payload)
    original = value.get(records_key, [])
    if not isinstance(original, list):
        original = []
    selected = []
    value[records_key] = selected
    value['returned_count'] = 0
    value['omitted_count'] = len(original)
    encode = lambda: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    for record in original[:max_records]:
        candidate = deepcopy(record)
        selected.append(candidate)
        value['returned_count'] = len(selected)
        value['omitted_count'] = len(original) - len(selected)
        if len(encode()) <= allowance:
            continue
        # Shorten a web excerpt before dropping a whole result. URLs stay whole.
        if isinstance(candidate, dict) and 'content' in candidate:
            excess = len(encode()) - allowance
            candidate['content'] = candidate['content'][:max(0, len(candidate['content']) - excess - 64)]
        if isinstance(candidate, dict) and 'quote' in candidate:
            lines = candidate['quote'].split('\n')
            while len(lines) > 1 and len(encode()) > allowance:
                hit = candidate.get('hit_line', candidate['line']) - candidate['line']
                if hit >= len(lines) // 2:
                    lines.pop(0)
                    candidate['line'] += 1
                else:
                    lines.pop()
                candidate['quote'] = '\n'.join(lines)
                candidate['end_line'] = candidate['line'] + len(lines) - 1
                candidate['read_file'] = {'file_path': candidate['path'], 'offset': candidate['end_line'] + 1}
            if len(lines) == 1 and len(encode()) > allowance:
                available = max(0, len(lines[0]) - (len(encode()) - allowance) - 200)
                start = max(0, candidate.get('hit_char', 0) - available // 2)
                candidate['quote'] = lines[0][start:start + available]
                candidate['start_char_offset'], candidate['end_char_offset'] = start, start + len(candidate['quote'])
                candidate['read_file'] = {'file_path': candidate['path'], 'offset': candidate['line'],
                                          'char_offset': candidate['end_char_offset']}
        if len(encode()) > allowance:
            selected.pop()
            break
    value['returned_count'] = len(selected)
    value['omitted_count'] = len(original) - len(selected)
    body = encode()
    if len(body) > allowance:
        body = ''  # Required execution/recovery facts remain in the envelope.
        selected = []
    more = selected != original
    result = page(source, body, returned_range={'offset': 0, 'count': len(selected)},
                  has_more=more,
                  next_cursor={'offset': 1, 'char_offset': 0} if more else None,
                  truncated_reason='page_limit' if more else None)
    result.page_renderer = lambda size: json_page(source, payload, size, records_key=records_key, max_records=max_records)
    result.original_payload = payload
    # The public result itself remains a valid JSON object, including at zero
    # allowance. Internal metadata is rendered under a legal content field.
    structured = json.loads(body) if body else {}
    structured['_output'] = facts(result)
    rendered = ToolOutput(json.dumps(structured, ensure_ascii=False, separators=(',', ':')))
    rendered.__dict__.update(result.__dict__)
    rendered.metadata_chars = len(rendered) - len(body)
    return rendered


def allocate(demands, available):
    """Water filling with stable remainder allocation in original call order."""
    result = [0] * len(demands)
    pending = list(range(len(demands)))
    while pending and available:
        share, remainder = divmod(available, len(pending))
        next_pending = []
        for rank, index in enumerate(pending):
            given = min(demands[index] - result[index], share + (rank < remainder))
            result[index] += given
            available -= given
            if result[index] < demands[index]:
                next_pending.append(index)
        pending = next_pending
    return result
