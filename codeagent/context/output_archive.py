"""Session-rooted immutable output snapshots with a shared UTF-8 byte cap."""
from __future__ import annotations

import json
import re
from pathlib import Path
import threading
import uuid

from codeagent.tools.output_limits import ARCHIVE_BYTES, BASH_BODY_CHARS
from codeagent.tools.output_pages import page


class OutputArchive:
    @classmethod
    def restore(cls, root, output_id):
        from codeagent.tools.runtime_data import _archive_path
        path = _archive_path(Path(root), output_id + '.txt')
        manifest_path = _archive_path(Path(root), output_id + '.json')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        result = cls.__new__(cls)
        result.root, result.path, result.output_id = Path(root), path, output_id
        result.saved_bytes, result.saved_chars = manifest['saved_bytes'], manifest['saved_chars']
        result.source_complete, result.reason = manifest['source_complete'], manifest['truncated_reason']
        result.channels, result.origin = manifest['channels'], manifest.get('origin')
        result.storage_error, result.handle, result.finished = None, None, True
        result.lock = threading.Lock()
        with path.open('r', encoding='utf-8', newline='') as stream:
            result.prefix = stream.read(BASH_BODY_CHARS)
        with path.open('rb') as stream:
            stream.seek(max(0, result.saved_bytes - BASH_BODY_CHARS * 4))
            result.tail = stream.read(BASH_BODY_CHARS * 4).decode('utf-8', errors='ignore')[-BASH_BODY_CHARS:]
        return result

    def __init__(self, root, *, origin=None):
        self.root = Path(root)
        self.output_id = uuid.uuid4().hex
        self.path = self.root / (self.output_id + '.txt')
        self.saved_bytes = self.saved_chars = 0
        self.source_complete = True
        self.reason = self.storage_error = None
        self.channels = []
        self.origin = origin
        self.prefix = self.tail = ''
        self.lock = threading.Lock()
        self.handle = None
        self.finished = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open('xb')
        except OSError as exc:
            self._failed(exc)

    def _failed(self, exc):
        self.storage_error = type(exc).__name__
        self.source_complete = False
        self.reason = 'storage_error'

    def append(self, text, channel='text'):
        with self.lock:
            if self.finished:
                return
            # Bounded deliverable evidence survives a failed archive.
            if len(self.prefix) < BASH_BODY_CHARS:
                self.prefix += text[:BASH_BODY_CHARS - len(self.prefix)]
            if not self.handle or self.storage_error:
                return
            if self.reason == 'archive_byte_limit':
                return
            remaining = ARCHIVE_BYTES - self.saved_bytes
            raw = text.encode('utf-8', errors='replace')
            if len(raw) > remaining:
                raw = raw[:remaining].decode('utf-8', errors='ignore').encode('utf-8')
                self.source_complete = False
                self.reason = 'archive_byte_limit'
            if not raw:
                return
            try:
                self.handle.write(raw)
            except OSError as exc:
                self._failed(exc)
                return
            saved = raw.decode('utf-8')
            start = self.saved_chars
            self.saved_chars += len(saved)
            self.saved_bytes += len(raw)
            self.tail = (self.tail + saved)[-BASH_BODY_CHARS:]
            if self.channels and self.channels[-1]['channel'] == channel:
                self.channels[-1]['end'] = self.saved_chars
            else:
                self.channels.append(dict(start=start, end=self.saved_chars, channel=channel))

    def finish(self, *, complete=True):
        with self.lock:
            if self.finished:
                return self
            self.finished = True
            self.source_complete &= complete
            if not complete and not self.reason:
                self.reason = 'collection_incomplete'
            try:
                if self.handle:
                    self.handle.close()
                if not self.storage_error:
                    manifest = dict(output_id=self.output_id, saved_bytes=self.saved_bytes,
                                    saved_chars=self.saved_chars, source_complete=self.source_complete,
                                    truncated_reason=self.reason, origin=self.origin,
                                    channel_order='collection_order; not exact process write order', channels=self.channels)
                    temporary = self.path.with_suffix('.json.tmp')
                    temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
                    temporary.replace(self.path.with_suffix('.json'))
            except OSError as exc:
                self._failed(exc)
        return self

    def attach(self, output):
        output.output_id = None if self.storage_error else self.output_id
        output.source_complete = self.source_complete
        output.storage_error = self.storage_error
        output.truncated_reason = self.reason
        output.archive = self
        return output


def archive_text(root, output, text=None, *, origin=None, content_type='text'):
    archive = OutputArchive(root, origin=origin)
    text = str(output) if text is None else text
    for start in range(0, len(text), 65536):
        archive.append(text[start:start + 65536])
        if archive.reason:
            break
    if content_type == 'json' and archive.reason == 'archive_byte_limit':
        archive.reason = 'archive_byte_limit: incomplete_json_text'
    return archive.finish().attach(output)


def log_page(output, allowance=BASH_BODY_CHARS):
    archive = output.archive
    _diagnostics(archive)
    # Pages always address saved content. Failed storage can only offer prefix.
    size = archive.saved_chars if not archive.storage_error else len(archive.prefix)
    ranges = []
    if size <= allowance:
        body = archive.prefix[:size]
        ranges = [{'start': 0, 'end': len(body)}] if body else []
    elif allowance < 160:
        body = ''
    elif output.status == 'error' and _diagnostics(archive):
        selected = []
        remaining = max(0, allowance - 100)
        for start, text in archive.diagnostics:
            fragment = text[:max(0, remaining - 80)]
            if not fragment:
                break
            selected.append((start, fragment))
            remaining -= len(fragment) + 80
        # Fill unused space with disjoint head/tail evidence, retaining order.
        head = remaining // 2
        tail = remaining - head
        if head:
            selected.append((0, archive.prefix[:head]))
        if tail:
            selected.append((size - tail, archive.tail[-tail:]))
        cursor, chunks = 0, []
        for start, text in sorted(selected):
            if start < cursor:
                text, start = text[cursor-start:], cursor
            if not text:
                continue
            end = start + len(text)
            chunks.append(f'[chars {start}:{end}]\n' + text)
            ranges.append({'start': start, 'end': end})
            cursor = end
        body = '\n'.join(chunks) + '\n[other log content omitted; recover by output_id]\n'
    else:
        marker = '\n[omitted; load_tool_output using output_id; positions are decoded archive characters]\n'
        available = max(0, allowance - len(marker) - 100)
        for _ in range(4):
            head, tail = available // 2, available - available // 2
            labels = f'[chars 0:{head}]\n' + f'[chars {size-tail}:{size}]\n'
            available = max(0, allowance - len(marker) - len(labels))
        head, tail = available // 2, available - available // 2
        ranges = [{'start': 0, 'end': head}, {'start': size - tail, 'end': size}]
        body = (f'[chars 0:{head}]\n' + archive.prefix[:head] + marker
                + f'[chars {size-tail}:{size}]\n' + archive.tail[-tail:])
    result = page(output, body, returned_range=ranges,
                  has_more=len(body) < size, next_cursor={'offset': 1, 'char_offset': 0} if len(body) < size else None,
                  saved_bytes=archive.saved_bytes,
                  recovery={'tool': 'load_tool_output', 'output_id': output.output_id},
                  channel_order='collection_order; channel ranges stored in archive manifest',
                  execution_note=str(output)[:256],
                  test_statistics=getattr(archive, 'test_statistics', None),
                  channels=archive.channels[:8])
    result.page_renderer = lambda limit: log_page(output, limit)
    return result


def _diagnostics(archive):
    if hasattr(archive, 'diagnostics'):
        return archive.diagnostics
    archive.diagnostics, archive.test_statistics = [], []
    if archive.storage_error:
        return []
    position, used, previous = 0, 0, ''
    remaining_after = 0
    with archive.path.open('r', encoding='utf-8', newline='') as handle:
        while text := handle.readline(8192):
            if re.search(r'^(?:Ran \d+ tests?|=+ .*\b(?:passed|failed|errors?)\b)', text):
                archive.test_statistics.append(text[:200].strip())
                archive.test_statistics = archive.test_statistics[:8]
            diagnostic = re.search(r'Traceback \(most recent call last\)|AssertionError|^FAIL:|^ERROR:|^FAILED |^E\s{2,}|^\s*assert ', text)
            if used < BASH_BODY_CHARS - 3000 and (diagnostic or remaining_after):
                if diagnostic and not remaining_after:
                    fragment = previous + text
                    archive.diagnostics.append((position - len(previous), fragment))
                    used += len(fragment)
                elif archive.diagnostics:
                    start, old = archive.diagnostics[-1]
                    archive.diagnostics[-1] = (start, old + text)
                    used += len(text)
                remaining_after = 3000 if diagnostic else max(0, remaining_after - len(text))
            previous = (previous + text)[-1500:]
            position += len(text)
    return archive.diagnostics
