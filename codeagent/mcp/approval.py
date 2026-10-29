"""Host-owned, deliberately narrow rules for the bundled browser preset."""

from urllib.parse import urlsplit
import math


def is_bundled_browser(server) -> bool:
    # Match the actual trusted launch contract, never the user-chosen alias or
    # a remote tool's self-reported annotations. Extra launch options opt out.
    if (server.transport != "stdio" or server.env or server.cwd is not None
            or server.command.lower() not in {"npx", "npx.cmd"}):
        return False
    args = list(server.args)
    if args[:1] == ["-y"]:
        args.pop(0)
    if not args or args.pop(0) != "@playwright/mcp@0.0.83":
        return False
    browser = False
    isolated = False
    while args:
        option = args.pop(0)
        if option == "--browser" and args and args.pop(0) in {"chrome", "msedge"} and not browser:
            browser = True
        elif option == "--isolated" and not isolated:
            isolated = True
        elif option != "--headless":
            return False
    return browser and isolated


def browser_read_allowed(server, remote_name: str, arguments: dict) -> bool:
    if not is_bundled_browser(server):
        return False
    if remote_name == "browser_snapshot":
        # The pinned implementation only reads with target/depth/boxes.
        # A filename writes to disk; unknown options must not inherit approval.
        return (set(arguments) <= {"target", "depth", "boxes"}
                and ("target" not in arguments or isinstance(arguments["target"], str))
                and ("depth" not in arguments or type(arguments["depth"]) in {int, float}
                     and math.isfinite(arguments["depth"]) and arguments["depth"] >= 0)
                and ("boxes" not in arguments or type(arguments["boxes"]) is bool))
    if remote_name == "browser_tabs":
        return arguments == {"action": "list"}
    if remote_name == "browser_wait_for":
        return (bool(arguments) and set(arguments) <= {"time", "text", "textGone"}
                and all(isinstance(v, str) for k, v in arguments.items() if k != "time")
                and ("time" not in arguments or type(arguments["time"]) in {int, float}
                     and 0 <= arguments["time"] <= 60))
    if remote_name == "browser_navigate" and set(arguments) == {"url"}:
        value = arguments["url"]
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            return False
        try:
            url = urlsplit(value)
            # Only ordinary site entry pages. Arbitrary paths, query strings,
            # local files and credentials can encode actions or sensitive data.
            return (url.scheme in {"http", "https"} and bool(url.hostname)
                    and not url.username and not url.password
                    and url.path in {"", "/"} and not url.query and not url.fragment
                    and "\\" not in value)
        except ValueError:
            return False
    return False
