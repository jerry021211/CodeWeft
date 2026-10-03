"""Source identity is independent of embedding content identity."""
from dataclasses import dataclass
import hashlib
import io
import tokenize
from pathlib import Path
from typing import Callable
from codeagent.runtime.execution import ExecutionStopped


def source_text(raw: bytes, path: str = 'source.py') -> str:
    if Path(path).suffix.lower() in ('.py', '.pyi'):
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        except SyntaxError as exc:
            raise UnicodeError('Invalid Python source encoding') from exc
    else:
        encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = raw.decode(encoding)
    if '\x00' in text:
        raise ValueError('Binary source is not searchable')
    return text


@dataclass(frozen=True)
class DocumentSnapshot:
    path: str
    language: str
    text: str
    content_hash: str

    @classmethod
    def read(cls, path, raw, language):
        return cls(path, language, source_text(raw, path), hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class CodeEntity:
    entity_id: str
    qualified_name: str
    kind: str
    signature: str
    definition_line: int
    container: str = ''


@dataclass(frozen=True)
class CodeChunk:
    entity: CodeEntity
    start_line: int
    end_line: int
    parser: str
    quality: str

    def document(self, snapshot):
        body = '\n'.join(snapshot.text.splitlines()[self.start_line - 1:self.end_line])
        entity = self.entity
        return dict(path=snapshot.path, language=snapshot.language, symbol=entity.qualified_name,
                    entity_id=entity.entity_id, parent=entity.entity_id, kind=entity.kind,
                    container=entity.container, signature=entity.signature,
                    definition_start_line=entity.definition_line, start_line=self.start_line,
                    end_line=self.end_line, body=body, content_hash=snapshot.content_hash,
                    body_hash=hashlib.sha256(body.encode()).hexdigest(),
                    parser=self.parser, parse_quality=self.quality,
                    comments='\n'.join(line for line in body.splitlines()
                        if line.lstrip().startswith(('#', '"', "'", '//', '/*', '*'))))


@dataclass(frozen=True)
class QueryContext:
    cancellation_check: Callable = lambda: None
    remaining_seconds: Callable = lambda: 120.
    client: object = None
    model: str | None = None
    emitter: object = None

    def check(self):
        self.cancellation_check()
        if self.remaining_seconds() <= 0:
            raise ExecutionStopped('budget_exceeded:active_time')
