"""Allow-listed git operations. Never runs arbitrary commands or force operations."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import os
import re
import subprocess

BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')


class GitError(Exception):
    pass


def owner_kwargs(root: Path) -> dict[str, Any]:
    """Under the root systemd helper, commands run as the repository owner so no file becomes root-owned."""
    if os.name == 'nt' or os.geteuid() != 0:
        return {}
    import pwd
    stat = root.stat()
    entry = pwd.getpwuid(stat.st_uid)
    return dict(user=stat.st_uid, group=stat.st_gid, extra_groups=[],
                env={**os.environ, 'HOME': entry.pw_dir, 'USER': entry.pw_name, 'LOGNAME': entry.pw_name})


def run_as_owner(root: Path, command: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    kwargs = owner_kwargs(root)
    env = {**kwargs.pop('env', os.environ), 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'}
    return subprocess.run(command, cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=timeout, env=env, **kwargs)


def git(root: Path, *args: str, timeout: float = 60) -> str:
    try:
        result = run_as_owner(root, ['git', *args], timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f'git {args[0]} vaxt limitini keçdi.') from None
    except OSError:
        raise GitError('git tapılmadı.') from None
    text = (result.stdout + result.stderr).decode('utf-8', 'replace').strip()
    if result.returncode:
        raise GitError(text[-2000:] or f'git {args[0]} uğursuz oldu.')
    return text


def info(root: Path) -> dict[str, Any]:
    current = git(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    commit = git(root, 'log', '-1', '--format=%h %s')
    dirty = bool(git(root, 'status', '--porcelain', '--untracked-files=no'))
    local = git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').splitlines()
    remote = [b.removeprefix('origin/') for b in
              git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin').splitlines()
              if b.startswith('origin/') and b != 'origin/HEAD']
    try:
        behind, ahead = git(root, 'rev-list', '--left-right', '--count', '@{upstream}...HEAD').split()
    except GitError:
        behind = ahead = None
    return dict(branch=current, commit=commit, dirty=dirty, branches=sorted(set(local) | set(remote)),
                behind=None if behind is None else int(behind), ahead=None if ahead is None else int(ahead))


def head(root: Path) -> str:
    return git(root, 'rev-parse', 'HEAD')


def changed(root: Path, before: str, after: str) -> list[str]:
    return git(root, 'diff', '--name-only', before, after).splitlines() if before != after else []


def fetch(root: Path) -> str:
    return git(root, 'fetch', '--prune', 'origin')


def pull(root: Path) -> str:
    if info(root)['dirty']:
        raise GitError('İşçi qovluqda commit edilməmiş dəyişikliklər var; pull edilmədi.')
    return git(root, 'pull', '--ff-only')


def switch(root: Path, branch: str) -> str:
    fetch(root)
    state = info(root)
    if not BRANCH.fullmatch(branch) or '..' in branch or branch not in state['branches']:
        raise GitError('Branch tapılmadı.')
    if state['dirty']:
        raise GitError('İşçi qovluqda commit edilməmiş dəyişikliklər var; branch dəyişdirilmədi.')
    # A remote-only branch gets a local tracking branch automatically.
    return git(root, 'switch', branch)
