"""Shared UTF-8 Markdown template loading; substitutions use str.format."""
from pathlib import Path


BUILTIN_TEMPLATE_DIR = Path(__file__).with_name("templates")


def load_template(name: str, *, directory: Path = BUILTIN_TEMPLATE_DIR) -> str:
    return (directory / f"{name}.md").read_text(encoding="utf-8").strip()
