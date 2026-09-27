"""Fixed pure-function checks, also usable as the Agent's narrow test tool.

Run in a short-lived process. Sources have no filesystem/network/process
capabilities; this restricted exercise is not a general Python sandbox.
"""
from __future__ import annotations

import ast
import builtins
import decimal
import json
from pathlib import Path
import sys

SAFE_BUILTINS = {name: getattr(builtins, name) for name in (
    "str", "int", "float", "bool", "list", "dict", "tuple", "set", "len", "range", "enumerate", "zip",
    "sum", "round", "min", "max", "abs", "sorted", "isinstance", "format", "ValueError", "TypeError")}


def pure_module(source):
    if len(source) > 30000:
        raise ValueError("Source exceeds exercise limit")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom)) and not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            raise ValueError("Only imports, docstrings and pure function definitions at module level")
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.Global, ast.Nonlocal, ast.With, ast.AsyncFunctionDef)):
            raise ValueError("Unsupported construct for pure-function exercise")
        if isinstance(node, (ast.Name, ast.Attribute)) and (node.id if isinstance(node, ast.Name) else node.attr).startswith("_"):
            raise ValueError("Private names are not part of the exercise API")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if any(m not in {"json", "decimal"} for m in modules) or getattr(node, "level", 0):
                raise ValueError("Only json and decimal imports are supported")
            if any(a.name.startswith("_") or a.asname and a.asname.startswith("_") for a in node.names):
                raise ValueError("Private import names are unsupported")
        if isinstance(node, ast.FunctionDef) and node.decorator_list:
            raise ValueError("Decorators are unsupported")

    def limited_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in {"json", "decimal"}:
            raise ValueError("Unsupported import")
        return {"json": json, "decimal": decimal}[name]

    namespace = {"__builtins__": {**SAFE_BUILTINS, "__import__": limited_import}}
    exec(compile(tree, "<exercise>", "exec"), namespace)
    return namespace


def check(workspace, suite):
    filename = {"parser": "parser.py", "exporter": "exporter.py", "totals": "totals.py"}[suite]
    root = Path(workspace).resolve()
    path = root / "src" / filename
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 30000:
            raise ValueError("Invalid source path/size")
        ns = pure_module(path.read_text("utf-8"))
        if suite == "parser":
            fn = ns["parse_record"]
            checks = [fn(" A1 | 10.20 \n") == {"id": "A1", "amount": "10.20"},
                      fn("B2|0") == {"id": "B2", "amount": "0"}]
        elif suite == "totals":
            fn = ns["total_amount"]
            checks = [abs(fn([{"amount": "10.20"}, {"amount": 3.3}]) - 13.5) < 1e-8, fn([]) == 0]
        else:
            fn = ns["export"]
            encoded = fn([{"id": "A1", "amount": "10.2"}, {"id": "中", "amount": 0}])
            checks = [type(encoded) is str, json.loads(encoded) ==
                      [{"order_id": "A1", "amount": "10.20"}, {"order_id": "中", "amount": "0.00"}],
                      json.loads(fn([])) == [],
                      json.loads(fn([{"id": "X-99", "amount": "-3.41"}])) == [{"order_id": "X-99", "amount": "-3.41"}]]
        return {"suite": suite, "passed": all(checks), "checks_passed": sum(checks), "checks_total": len(checks)}
    except Exception as exc:
        return {"suite": suite, "passed": False, "error_type": type(exc).__name__}


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1], sys.argv[2])))
