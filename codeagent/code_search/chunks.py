"""AST spans and tokenization. All locations refer to unchanged source lines."""
from __future__ import annotations

import ast
import hashlib
import io
import tokenize

from codeagent.memory.retrieval import terms


def encoded(text: str) -> str:
    return " ".join("t" + term.encode("utf-8").hex() for term in terms(text))


def source_text(raw: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding)


def chunks(path: str, raw: bytes, check=lambda: None) -> list[dict]:
    text = source_text(raw)
    lines = text.splitlines()
    digest = hashlib.sha256(raw).hexdigest()
    result = []

    def add(symbol, definition, start, end, kind, signature=""):
        check()
        body = "\n".join(lines[start - 1:end])
        comments = "\n".join(line for line in body.splitlines() if line.lstrip().startswith(("#", '"', "'")))
        result.append(dict(path=path, symbol=symbol, definition_start_line=definition,
                           start_line=start, end_line=end, kind=kind,
                           signature=signature, comments=comments, body=body,
                           content_hash=digest, parent=f"{path}::{symbol}@{definition}"))

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        for start in range(1, len(lines) + 1, 40):
            add("<text>", start, start, min(start + 39, len(lines)), "parse_fallback")
        return result

    def visit(node, parents=()):
        check()
        named = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        scope = (*parents, node.name) if named else parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol = ".".join(scope)
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno
            signature = "\n".join(lines[node.lineno - 1:node.body[0].lineno])[:1500]
            if end - start < 60:
                add(symbol, node.lineno, start, end, "function", signature)
            else:
                # Prefer statement boundaries; split oversized statements as a fallback.
                boundaries = sorted({start, end + 1, *(s.lineno for s in node.body)})
                cursor = start
                while cursor <= end:
                    stop = max((b for b in boundaries if cursor < b <= cursor + 60), default=min(cursor + 60, end + 1))
                    add(symbol, node.lineno, cursor, stop - 1, "function", signature)
                    cursor = stop
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree)
    for statement in tree.body:
        if not isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for start in range(statement.lineno, statement.end_lineno + 1, 40):
                add("<module>", statement.lineno, start, min(start + 39, statement.end_lineno), "module")
    return result
