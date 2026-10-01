"""Freeze a source corpus and separate public queries from private qrels."""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from evals.evidence import file_hash, read_json, seal, snapshot, verify_seal, write_json
from . import VERSION
from .cases import ABSENCE_PATTERNS, CASES, SOURCES


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def source_files(source: Path) -> list[Path]:
    # Fixed, declared Python-only scope. No evals, reports, credentials or run logs.
    return sorted(p for folder in ("codeagent", "tests") for p in (source / folder).rglob("*.py")
                  if "__pycache__" not in p.parts and not p.is_symlink()
                  and p.resolve().is_relative_to(source.resolve())
                  and not p.name.startswith("test_code_retrieval"))


def functions(source: Path, paths: list[Path]) -> list[dict]:
    result = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text, filename=str(path))

        def visit(node, parents=()):
            for child in ast.iter_child_nodes(node):
                named = isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                names = parents + (child.name,) if named else parents
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    relative = path.relative_to(source).as_posix()
                    symbol = ".".join(names)
                    body = "\n".join(lines[child.lineno - 1:child.end_lineno])
                    result.append({"_id": f"{relative}::{symbol}@{child.lineno}", "title": symbol,
                                   "text": body, "path": relative, "symbol": symbol,
                                   "start_line": child.lineno, "end_line": child.end_lineno,
                                   "source_sha256": file_hash(path)})
                visit(child, names)
        visit(tree)
    return result


def compile_cases(source: Path, docs: list[dict]) -> list[dict]:
    compiled = []
    for spec in CASES:
        groups = []
        for index, alternatives in enumerate(spec["groups"]):
            members = []
            for target in alternatives:
                matches = [d for d in docs if d["path"] == target["path"] and d["symbol"] == target["symbol"]]
                if len(matches) != 1:
                    raise ValueError(f"{spec['id']}: missing/ambiguous symbol {target}")
                doc = matches[0]
                lines = doc["text"].splitlines()
                anchors = []
                for needle_index, needle in enumerate(target["needles"]):
                    found = [doc["start_line"] + i for i, line in enumerate(lines) if needle in line]
                    if not found:
                        raise ValueError(f"{spec['id']}: source drift; evidence missing: {needle}")
                    # First needle is the behavior-specific scoring anchor. The
                    # remaining needles check source drift, not generic matches.
                    if needle_index == 0:
                        anchors.extend(found)
                members.append({"doc_id": doc["_id"], "evidence_lines": sorted(set(anchors))})
            groups.append({"id": f"g{index + 1}", "members": members})
        if spec["category"] == "cross_file":
            ids = {m["doc_id"] for g in groups for m in g["members"]}
            if len({d["path"] for d in docs if d["_id"] in ids}) < 2:
                raise ValueError(f"{spec['id']}: cross-file question requires at least two files")
        if spec["id"] in ABSENCE_PATTERNS:
            pattern = re.compile(ABSENCE_PATTERNS[spec["id"]], re.I)
            hits = [p.relative_to(source).as_posix() for p in (source / "codeagent").rglob("*.py")
                    if pattern.search(p.read_text(encoding="utf-8"))]
            if hits:
                raise ValueError(f"{spec['id']}: absence annotation needs renewed source review: {hits}")
        compiled.append({**{k: v for k, v in spec.items() if k != "groups"}, "groups": groups})
    return compiled


def validate_source(source: Path) -> dict:
    docs = functions(source, source_files(source))
    cases = compile_cases(source, docs)
    return {"valid": True, "documents": len(docs), "cases": len(cases),
            "test_categories": dict(Counter(c["category"] for c in cases if c["split"] == "test")),
            "dev_cases": sum(c["split"] == "dev" for c in cases)}


def prepare(source: Path, output: Path) -> Path:
    source, output = source.resolve(), output.resolve()
    docs = functions(source, source_files(source))
    cases = compile_cases(source, docs)
    output.mkdir(parents=True, exist_ok=False)
    target = output / "target"
    for path in source_files(source):
        copied = target / path.relative_to(source)
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
    for name in ("pyproject.toml", ".gitignore"):
        if (source / name).is_file():
            shutil.copyfile(source / name, target / name)
    target_hashes = {p.relative_to(target).as_posix(): file_hash(p) for p in target.rglob("*") if p.is_file()}
    public = [{"_id": c["id"], "text": c["query"], "split": c["split"], "category": c["category"]} for c in cases]
    write_jsonl(output / "corpus.jsonl", docs)
    write_jsonl(output / "queries.jsonl", public)
    (output / "questions.md").write_text("# 冻结题目（不含答案）\n\n" + "\n\n".join(
        f"**{c['id']} · {c['split']} · {c['category']}**\n\n{c['query']}" for c in cases) + "\n", encoding="utf-8")
    write_json(output / "gold.json", {"version": VERSION, "cases": cases})
    rows = ["query-id\tcorpus-id\tscore"]
    for c in cases:
        for doc_id in sorted({m["doc_id"] for g in c["groups"] for m in g["members"]}):
            rows.append(f"{c['id']}\t{doc_id}\t1")
    (output / "qrels.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    engine = snapshot(source, output / "baseline-engine")
    write_json(output / "manifest.json", {
        "version": VERSION, "source_root": str(source),
        "corpus_scope": "codeagent/**/*.py + tests/**/*.py, excluding test_code_retrieval*.py",
        "target_hashes": target_hashes, "corpus_hash": digest(target_hashes),
        "queries_hash": digest(public), "gold_hash": digest(cases), "baseline_engine": engine,
        "references": SOURCES, "annotation_status": "assistant_source_reviewed_not_independent_human_review",
        "case_counts": dict(Counter(c["split"] for c in cases)),
    })
    seal(output)
    return output


def load_bundle(bundle: Path):
    if not verify_seal(bundle)["valid"]:
        raise ValueError("Frozen bundle changed; prepare a new bundle rather than overwrite evidence")
    return read_json(bundle / "manifest.json"), read_jsonl(bundle / "corpus.jsonl"), read_json(bundle / "gold.json")["cases"]


def initialize_target(target: Path) -> None:
    # A real Git working tree preserves the default tools' ignored/untracked semantics.
    subprocess.run(["git", "init", "-q", str(target)], check=True, capture_output=True, timeout=15)
