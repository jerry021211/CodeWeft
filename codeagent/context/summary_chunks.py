"""Lossless field slices for summary material; canonical messages stay intact."""
from __future__ import annotations

import json
from codeagent.context.budget import inspect_request, enforce_request, RequestBudgetError
from codeagent.context.summary_source import summary_source_messages
from codeagent.tools.output_limits import SUMMARY_BLOCKS, SUMMARY_BLOCK_TOKENS


def fields(value, path=''):
    if isinstance(value, dict):
        if not value:
            yield path, {}
        for key, child in value.items():
            yield from fields(child, path + '/' + str(key))
    elif isinstance(value, list):
        if not value:
            yield path, []
        for index, child in enumerate(value):
            yield from fields(child, path + '/' + str(index))
    else:
        yield path, value


def chunk_requests(messages, make_request, window):
    chunks, current = [], []
    def fits(records):
        raw = json.dumps(records, ensure_ascii=False, separators=(',', ':'))
        material = inspect_request(model='', system='', tools=[], max_tokens=0,
                                   messages=[{'role': 'user', 'content': raw}])
        if material.estimated_prompt_tokens > SUMMARY_BLOCK_TOKENS:
            return False
        try:
            enforce_request(**make_request(raw), context_window_tokens=window)
        except RequestBudgetError:
            return False
        return True
    def flush():
        nonlocal current
        if current:
            chunks.append(make_request(json.dumps(current, ensure_ascii=False, separators=(',', ':'))))
            current = []
        if len(chunks) > SUMMARY_BLOCKS:
            raise ValueError('summary material requires more than 16 complete blocks')
    source = summary_source_messages(messages)
    if fits(source):
        return [make_request(json.dumps(source, ensure_ascii=False, separators=(',', ':')))]
    for index, message in enumerate(source):
        for field, value in fields(message):
            identity = {'message_index': index, 'role': message.get('role'), 'field': field}
            if not isinstance(value, str):
                record = {**identity, 'value': value}
                if not fits([*current, record]):
                    flush()
                if not fits([record]):
                    raise ValueError('Summary fixed instructions/output reserve do not fit window')
                current.append(record)
                continue
            position = 0
            while position < len(value) or (position == 0 and not value):
                end = len(value)
                record = {**identity, 'start': position, 'end': end, 'text': value[position:end]}
                if fits([*current, record]):
                    current.append(record)
                    break
                if current:
                    flush()
                    continue
                low, high = position + 1, end
                best = position
                while low <= high:
                    middle = (low + high) // 2
                    piece = {**identity, 'start': position, 'end': middle, 'text': value[position:middle]}
                    if fits([piece]):
                        best, low = middle, middle + 1
                    else:
                        high = middle - 1
                if best == position:
                    raise ValueError('Summary fixed instructions/output reserve do not fit window')
                current.append({**identity, 'start': position, 'end': best, 'text': value[position:best]})
                position = best
                flush()
    if current:
        chunks.append(make_request(json.dumps(current, ensure_ascii=False, separators=(',', ':'))))
    if len(chunks) > SUMMARY_BLOCKS:
        raise ValueError('summary material exceeds 16 blocks')
    return chunks
