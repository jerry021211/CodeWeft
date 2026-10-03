"""Project roots are bounded by the actual workspace, including worktrees."""
import hashlib
from pathlib import Path
from codeagent.runtime.data_paths import RuntimeDataPaths


def project_root(guard, path, language, *, markers=(), aggregate=False):
    markers = markers or (('pom.xml', 'build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts')
               if language == 'java' else ('tsconfig.json', 'jsconfig.json', 'package.json')
               if language.startswith('type') or language in ('javascript', 'javascriptreact')
               else ('pyproject.toml', 'pyrightconfig.json', 'setup.py'))
    found = []
    current = path.parent
    while current.is_relative_to(guard.root):
        if any(guard.allows(current / marker) and (current / marker).is_file() for marker in markers):
            found.append(current)
        if current == guard.root:
            break
        current = current.parent
    return (found[-1] if language == 'java' or aggregate else found[0]) if found else guard.root


def server_command(server, workspace, root, session_id):
    command = list(server.command)
    if server.name == 'jdtls' or any('{data_dir}' in part for part in command):
        # Per-owner subdirectory prevents independent Agent JVMs locking one data
        # directory; the workspace/project prefix also isolates worktrees.
        fingerprint = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        directory = RuntimeDataPaths.default().workspace_dir(workspace) / 'lsp' / fingerprint / session_id
        directory.mkdir(parents=True, exist_ok=True)
        command = [part.replace('{data_dir}', str(directory)) for part in command]
        if '-data' not in command:
            command += ['-data', str(directory)]
    return tuple(command)
