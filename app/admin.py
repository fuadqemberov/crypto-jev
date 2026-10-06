"""Allow-listed git operations for the dashboard. Never runs arbitrary commands or force operations."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import asyncio
import os
import re
import subprocess

BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')


class GitError(Exception):
    pass


async def git(root: Path, *args: str, timeout: float = 60) -> str:
    # A worker thread works on every event loop (Windows selector loops cannot spawn subprocesses).
    try:
        result = await asyncio.to_thread(
            subprocess.run, ['git', *args], cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=timeout, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'})
    except subprocess.TimeoutExpired:
        raise GitError(f'git {args[0]} vaxt limitini keçdi.') from None
    except OSError:
        raise GitError('git tapılmadı.') from None
    text = (result.stdout + result.stderr).decode('utf-8', 'replace').strip()
    if result.returncode:
        raise GitError(text[-2000:] or f'git {args[0]} uğursuz oldu.')
    return text


async def info(root: Path, fetch: bool = False) -> dict[str, Any]:
    if fetch:
        await git(root, 'fetch', '--prune', 'origin')
    current = await git(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    commit = await git(root, 'log', '-1', '--format=%h %s')
    dirty = bool(await git(root, 'status', '--porcelain', '--untracked-files=no'))
    local = (await git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/heads')).splitlines()
    remote = [b.removeprefix('origin/') for b in
              (await git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin')).splitlines()
              if b.startswith('origin/') and b != 'origin/HEAD']
    try:
        behind, ahead = (await git(root, 'rev-list', '--left-right', '--count', '@{upstream}...HEAD')).split()
    except GitError:
        behind = ahead = None
    return dict(branch=current, commit=commit, dirty=dirty, branches=sorted(set(local) | set(remote)),
                behind=None if behind is None else int(behind), ahead=None if ahead is None else int(ahead))


async def pull(root: Path) -> str:
    if (await info(root))['dirty']:
        raise GitError('İşçi qovluqda commit edilməmiş dəyişikliklər var; pull edilmədi.')
    return await git(root, 'pull', '--ff-only')


async def switch(root: Path, branch: str) -> str:
    state = await info(root, fetch=True)
    if not BRANCH.fullmatch(branch) or '..' in branch or branch not in state['branches']:
        raise GitError('Branch tapılmadı.')
    if state['dirty']:
        raise GitError('İşçi qovluqda commit edilməmiş dəyişikliklər var; branch dəyişdirilmədi.')
    # A remote-only branch gets a local tracking branch automatically.
    return await git(root, 'switch', branch)
