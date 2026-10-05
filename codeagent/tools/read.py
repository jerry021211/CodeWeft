"""Versioned, bounded, continuous source-file pages."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
from codeagent.tools.base import ToolDefinition, ToolOutput, parameter_error
from codeagent.tools.output_limits import READ_BODY_CHARS, READ_SOURCE_LINES, RESULT_METADATA_CHARS
from codeagent.tools.workspace import WorkspaceGuard

PAGE_MARKER = '[read_file page] '


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _hash(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    while block := handle.read(65536):
        digest.update(block)
    return digest.hexdigest()


def _peek(stream):
    position = stream.tell()
    value = stream.read(1)
    stream.seek(position)
    return value


def _fragment(stream, size):
    """Bound readline memory, including CRLF at a chunk boundary."""
    text = stream.readline(size)
    if text.endswith('\r') and _peek(stream) == '\n':
        text += stream.read(1)
    return text


def _page(stream, *, offset, char_offset, limit, body_limit, positioned=False):
    """One paginator for live reads and shrinking captured pages.

    Decoded offsets include original line terminators. Number prefixes are
    display-only. CRLF stays intact, so a continuation cannot invent a line.
    """
    if not positioned:
        line = 1
        while line < offset:
            part = _fragment(stream, 8192)
            if not part:
                return '', '', None, None, False, None, None
            if part.endswith(('\n', '\r')):
                line += 1
        remaining = char_offset
        while remaining:
            position = stream.tell()
            part = _fragment(stream, min(8192, remaining))
            if len(part) > remaining:
                # Explicit positions between CR/LF are valid decoded positions.
                stream.seek(position)
                stream.read(remaining)
                remaining = 0
                break
            if not part or (part.endswith(('\n', '\r')) and len(part) < remaining):
                raise ValueError('char_offset exceeds the starting line')
            remaining -= len(part)
            if part.endswith(('\n', '\r')):
                offset += 1
                char_offset = 0
    body, raw = [], []
    used, count = 0, 0
    start = end = None
    line, column = offset, char_offset
    while count < limit:
        prefix = f'{line}\t'
        available = body_limit - used - len(prefix)
        if available <= 0:
            break
        part = _fragment(stream, READ_BODY_CHARS + 1)
        if not part:
            break
        complete_line = part.endswith(('\n', '\r')) or not _peek(stream)
        if len(part) > available and complete_line and len(part) + len(prefix) <= body_limit and body:
            return ''.join(body), ''.join(raw), start, end, True, line, column
        shown = part[:available]
        if shown.endswith('\r') and len(shown) < len(part) and part[len(shown)] == '\n':
            shown = shown[:-1]
        if not shown:
            return ''.join(body), ''.join(raw), start, end, True, line, column
        if start is None:
            start = (line, column)
        end = (line, column + len(shown))
        body.append(prefix + shown)
        raw.append(shown)
        used += len(prefix) + len(shown)
        count += 1
        if len(shown) < len(part) or not complete_line:
            return ''.join(body), ''.join(raw), start, end, True, line, column + len(shown)
        line, column = line + 1, 0
    more = bool(_peek(stream))
    return ''.join(body), ''.join(raw), start, end, more, line if more else None, column if more else None


def _output(page, *, version, snapshot, requested_offset, requested_char_offset, requested_limit,
            source_more=False, source_next=None):
    body, raw, start, end, more, next_line, next_char = page
    if not more and source_more:
        more = True
        next_line, next_char = source_next
    metadata = dict(source_kind='live_file', source_version=version,
                    start_line=start[0] if start else None, start_char_offset=start[1] if start else None,
                    end_line=end[0] if end else None, end_char_offset=end[1] if end else None,
                    has_more=more, next_offset=next_line, next_char_offset=next_char,
                    eof=start is None and not more, body_chars=len(body),
                    source_complete=True, range_complete=not more or (next_line is not None and next_line >= requested_offset + requested_limit),
                    continuation='Carry source_version as expected_version; on mismatch relocate before reading.')
    metadata.update(status='success', exit_code=None, outcome='', process_id=None, output_id=None,
                    returned_range={'start': list(start) if start else None, 'end': list(end) if end else None},
                    next_cursor={'offset': next_line, 'char_offset': next_char, 'expected_version': version} if more else None,
                    truncated_reason='page_limit' if more else None)
    header = PAGE_MARKER + json.dumps(metadata, ensure_ascii=False, separators=(',', ':')) + '\n'
    if len(header) > RESULT_METADATA_CHARS:
        raise ValueError('read page metadata exceeds fixed reservation')
    result = ToolOutput(header + body)
    result.file_read_snapshot = {**snapshot, 'source_version': version,
        'output_hash': hashlib.sha256(str(result).encode('utf-8')).hexdigest(), 'actual_range': metadata}
    result.read_page = dict(raw=raw, metadata=metadata, snapshot=result.file_read_snapshot,
                           requested_offset=requested_offset, requested_char_offset=requested_char_offset,
                           requested_limit=requested_limit)
    result.returned_range = {key: metadata[key] for key in ('start_line', 'start_char_offset', 'end_line', 'end_char_offset')}
    result.next_cursor = {'offset': next_line, 'char_offset': next_char, 'expected_version': version} if more else None
    result.has_more, result.source_complete = more, True
    result.source_version = version
    result.body = body
    result.metadata_chars = len(header)
    result.page_ready = True
    result.page_renderer = lambda size: resize_read_page(result, size + RESULT_METADATA_CHARS)
    return result


def resize_read_page(output, max_chars):
    """Re-page captured text only: no new filesystem access or execution."""
    data = getattr(output, 'read_page', None)
    if not data or len(output.body) <= max(0, max_chars - RESULT_METADATA_CHARS):
        return output
    prefix, suffix = data.get('feedback', ('', ''))
    allowance = min(READ_BODY_CHARS, max_chars - RESULT_METADATA_CHARS - len(prefix) - len(suffix))
    meta = data['metadata']
    if allowance <= len(str(meta['start_line'])) + 2 and meta['start_line'] is not None:
        page = ('', '', None, None, True, data['requested_offset'], data['requested_char_offset'])
        return _output(page, version=meta['source_version'], snapshot=data['snapshot'],
                       requested_offset=data['requested_offset'], requested_char_offset=data['requested_char_offset'],
                       requested_limit=data['requested_limit'])
    if meta['start_line'] is None:
        return output
    page = _page(io.StringIO(data['raw'], newline=''), offset=meta['start_line'],
                 char_offset=meta['start_char_offset'], limit=data['requested_limit'],
                 body_limit=allowance, positioned=True)
    result = _output(page, version=meta['source_version'], snapshot=data['snapshot'],
                   requested_offset=data['requested_offset'], requested_char_offset=data['requested_char_offset'],
                   requested_limit=data['requested_limit'], source_more=meta['has_more'],
                   source_next=(meta['next_offset'], meta['next_char_offset']))
    return with_read_feedback(result, prefix, suffix)


def with_read_feedback(output, prefix='', suffix=''):
    if not prefix and not suffix:
        return output
    if getattr(output, 'read_page', None):
        return _read_feedback(output, prefix + suffix)
    result = ToolOutput(prefix + str(output) + suffix)
    result.__dict__.update(getattr(output, '__dict__', {}))
    result.feedback = (prefix, suffix)
    if getattr(output, 'read_batch', None):
        result.metadata_chars += len(prefix) + len(suffix)
        result.page_renderer = lambda size: with_read_feedback(output.page_renderer(size), prefix, suffix)
    if getattr(output, 'read_page', None):
        result.read_page = {**output.read_page, 'feedback': (prefix, suffix)}
    return result


def _read_feedback(output, feedback):
    metadata = {**output.read_page['metadata'], 'feedback': feedback[:512]}
    header = PAGE_MARKER + json.dumps(metadata, ensure_ascii=False, separators=(',', ':')) + '\n'
    if len(header) > RESULT_METADATA_CHARS:
        metadata.pop('feedback')
        header = PAGE_MARKER + json.dumps(metadata, ensure_ascii=False, separators=(',', ':')) + '\n'
    result = ToolOutput(header + output.body)
    result.__dict__.update(output.__dict__)
    result.metadata_chars = len(header)
    result.file_read_snapshot = {**output.file_read_snapshot, 'actual_range': metadata,
                                 'output_hash': hashlib.sha256(str(result).encode('utf-8')).hexdigest()}
    result.read_page = {**output.read_page, 'metadata': metadata, 'snapshot': result.file_read_snapshot}
    result.page_renderer = lambda size: _read_feedback(output.page_renderer(size), feedback)
    return result


@dataclass(frozen=True, slots=True)
class ReadFileTool:
    definition: ToolDefinition = ToolDefinition(
        name='read_file', effect='read', reentrant=True,
        description=(f'读取连续原文，正文最多{READ_BODY_CHARS}字符（含行号），最多{READ_SOURCE_LINES}行。续读使用next_offset/next_char_offset，'
                     'file_path读取单文件；file_paths一次读取1至5个已知相关文件，二者仅选一个。批量正文共享字符预算，每个文件分别保留行号和续读信息。'
                     '并必须携带上一页source_version作为expected_version；版本变化后重新定位。'
                     '行内偏移按解码字符计（含原换行），结束位置为排他边界。引用代码时去掉显示行号并使用实际行号。'
                     'force_full只禁用重复读取短引用。'),
        input_schema={'type': 'object', 'properties': {
            'file_path': {'type': 'string', 'description': '文件路径'},
            'file_paths': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 5,
                           'description': '一次读取最多5个文件。offset/limit应用于每个文件；续读改用单文件file_path和该文件的版本及游标。'},
            'offset': {'type': 'integer', 'minimum': 1, 'default': 1},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': READ_SOURCE_LINES, 'default': READ_SOURCE_LINES},
            'char_offset': {'type': 'integer', 'minimum': 0, 'default': 0, 'description': '仅起始行的行内字符位置，不含显示行号。'},
            'expected_version': {'type': 'string', 'description': '续读时传上一页source_version；不匹配则拒绝正文。'},
            'force_full': {'type': 'boolean', 'default': False}},
            'oneOf': [{'required': ['file_path']}, {'required': ['file_paths']}]})
    workspace_guard: WorkspaceGuard | None = None

    def run(self, file_path: str | None = None, offset: int = 1, limit: int = READ_SOURCE_LINES,
            force_full: bool = False, char_offset: int = 0, expected_version: str | None = None,
            file_paths: list[str] | None = None) -> str:
        if (file_path is None) == (file_paths is None):
            return parameter_error('Provide exactly one of file_path or file_paths.', 'read_file:paths')
        if file_paths is not None:
            if (not isinstance(file_paths, list) or not 1 <= len(file_paths) <= 5
                    or any(not isinstance(p, str) or not p.strip() for p in file_paths)):
                return parameter_error('file_paths must contain 1 to 5 non-empty paths.', 'read_file:file_paths')
            if expected_version is not None or char_offset != 0:
                return parameter_error('Continue each file separately with file_path and its own cursor/version.', 'read_file:batch_cursor')
            from codeagent.tools.read_batch import render_batch
            return render_batch([(path, self.run(path, offset, limit, force_full)) for path in file_paths])
        if not isinstance(file_path, str) or not file_path.strip():
            return parameter_error('file_path must be a non-empty path.', 'read_file:file_path')
        if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= READ_SOURCE_LINES:
            return parameter_error(f'offset必须为正整数；limit必须为1到{READ_SOURCE_LINES}的整数。', 'read_file:positive_range')
        if type(char_offset) is not int or char_offset < 0:
            return parameter_error('char_offset必须为非负整数。', 'read_file:char_offset')
        if type(force_full) is not bool:
            return parameter_error('force_full必须为布尔值。', 'read_file:force_full:boolean')
        if expected_version is not None and (not isinstance(expected_version, str) or not expected_version):
            return parameter_error('expected_version必须为非空版本字符串。', 'read_file:version')
        try:
            path = self.workspace_guard.resolve(file_path) if self.workspace_guard else Path(file_path).expanduser().resolve()
            if not path.is_file():
                return ToolOutput(f'Error: {file_path} not found or not a file', status='error')
            path_before = _stamp(path.stat())
            with path.open('rb') as handle:
                before = _stamp(os.fstat(handle.fileno()))
                digest = _hash(handle)
                version = hashlib.sha256(json.dumps([path_before, digest]).encode()).hexdigest()
                if expected_version is not None and expected_version != version:
                    return ToolOutput('Error: file changed (source_version mismatch); relocate and restart reading.', status='error', outcome='source_changed')
                handle.seek(0)
                stream = io.TextIOWrapper(handle, encoding='utf-8', errors='replace', newline='')
                try:
                    page = _page(stream, offset=offset, char_offset=char_offset, limit=limit, body_limit=READ_BODY_CHARS)
                finally:
                    stream.detach()
                final_digest = _hash(handle)
                after = _stamp(os.fstat(handle.fileno()))
            current = _stamp(path.stat())
            if self.workspace_guard:
                self.workspace_guard.ensure_within(path)
            # Windows path.stat and fstat may expose different ctime semantics.
            # Compare each source over time; cross-check identity/size/mtime.
            if path_before[:4] != before[:4] or before != after or path_before != current or after[:4] != current[:4] or digest != final_digest:
                return ToolOutput('Error: file changed during read; page discarded. Relocate and restart reading.', status='error', outcome='source_changed')
            snapshot = dict(version=1, path=str(path), file_hash=digest, file_stamp=current,
                            offset=offset, limit=limit, char_offset=char_offset, force_full=force_full)
            return _output(page, version=version, snapshot=snapshot, requested_offset=offset,
                           requested_char_offset=char_offset, requested_limit=limit)
        except Exception as exc:
            return ToolOutput(f'Error: {exc}', status='error')
