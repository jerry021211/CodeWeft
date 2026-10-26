"""List or explicitly install CodeWeft's starter skills."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeagent.skills import SkillLoader
from codeagent.skills.bundled import bundled_root, install_bundled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List packaged skills without modifying the global library")
    install = commands.add_parser("install", help="Install starter skills, preserving existing directories")
    install.add_argument("names", nargs="*", help="Exact names; omit to install all")
    install.add_argument("--destination", type=Path, help="Default: first configured global SKILLS_DIR")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(SkillLoader([bundled_root()]).catalog_prompt())
        return 0
    destination = args.destination
    if destination is None:
        from codeagent.config import EnvironmentConfig
        from codeagent.runtime import RuntimeDataPaths

        env = EnvironmentConfig.from_env()
        if not env.skill_roots:
            parser.error("No configured skill root; supply --destination")
        destination = RuntimeDataPaths(env.data_dir).skill_root(env.skill_roots[0])
    try:
        result = install_bundled(destination, args.names or None)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Skill installation failed: {exc}\n")
    print(json.dumps({"destination": str(destination.resolve()), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
