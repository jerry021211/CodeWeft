"""Narrow runtime artifact policy; never hide tracked or baseline source files."""
import os
from pathlib import PurePosixPath


def generated_cache(name):
    parts = PurePosixPath(name).parts
    if '__pycache__' in parts and parts[-1].endswith('.pyc'):
        return True
    if '.pytest_cache' in parts:
        suffix = parts[parts.index('.pytest_cache') + 1:]
        return suffix in {('CACHEDIR.TAG',), ('README.md',), ('.gitignore',),
                          ('v', 'cache', 'nodeids'), ('v', 'cache', 'lastfailed'), ('v', 'cache', 'stepwise')}
    return False


def cache_paths(git, path, base):
    protected = set(git.run('ls-tree', '-r', '--name-only', '-z', base, cwd=path).stdout.split(b'\0'))
    protected.update(git.run('ls-files', '--cached', '-z', cwd=path).stdout.split(b'\0'))
    untracked = git.run('ls-files', '--others', '--exclude-standard', '-z', cwd=path).stdout.split(b'\0')
    return [os.fsdecode(name) for name in untracked
            if name and name not in protected and generated_cache(os.fsdecode(name))]
