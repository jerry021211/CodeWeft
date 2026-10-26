"""Command-line launcher for the local CodeWeft workbench."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


_RELOAD_WORKSPACE_ENV = "_CODEAGENT_RELOAD_WORKSPACE"


def create_reload_app():
    """Build resources in the server child, never in the reload supervisor."""
    from codeagent.web.api import create_app

    return create_app(workspace=Path(os.environ[_RELOAD_WORKSPACE_ENV]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local CodeWeft web UI.")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--reload", action="store_true",
        help="Restart on CodeWeft Python source changes (development only; interrupts active tasks).",
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace is not a directory: {workspace}")
    try:
        import uvicorn
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Unable to load the web server runtime. Install or repair it with: "
            "pip install -e .[web]"
        ) from exc

    if args.reload:
        # Spawned workers must retain --workspace even when it differs from cwd.
        previous = os.environ.get(_RELOAD_WORKSPACE_ENV)
        os.environ[_RELOAD_WORKSPACE_ENV] = str(workspace)
        try:
            uvicorn.run(
                "codeagent.web.cli:create_reload_app",
                factory=True, host=args.host, port=args.port, reload=True,
                reload_dirs=[str(Path(__file__).resolve().parents[1])],
                # Persistent SSE responses must not block lifespan cleanup forever.
                timeout_graceful_shutdown=5,
            )
        finally:
            if previous is None:
                os.environ.pop(_RELOAD_WORKSPACE_ENV, None)
            else:
                os.environ[_RELOAD_WORKSPACE_ENV] = previous
    else:
        from codeagent.web.api import create_app

        app = create_app(workspace=workspace)
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
