"""Install the packaged starter library without replacing user skills."""
from __future__ import annotations

from pathlib import Path

from codeagent.skills.loader import SkillLoader


def bundled_root() -> Path:
    return Path(__file__).parent / "library"


def install_bundled(destination: Path, names: list[str] | None = None) -> dict[str, list[str]]:
    """Copy selected manifests; an existing skill directory always wins.

    Installation is explicit, never a side effect of opening a workspace.
    Validate all names before creating anything. A repeat install is a no-op.
    """
    loader = SkillLoader([bundled_root()])
    available = {skill.name: skill for skill in loader.list_skills()}
    selected = sorted(available if names is None else set(names))
    unknown = set(selected) - available.keys()
    if unknown:
        raise ValueError(f"Unknown bundled skills: {', '.join(sorted(unknown))}")
    contents = {name: loader.load(name).content for name in selected}
    destination = Path(destination).expanduser().resolve()
    result: dict[str, list[str]] = {"installed": [], "skipped": []}
    for name in selected:
        directory = destination / name
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            result["skipped"].append(name)
            continue
        # Exclusive creation protects a concurrently created manifest too.
        with (directory / "SKILL.md").open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(contents[name])
        result["installed"].append(name)
    return result
