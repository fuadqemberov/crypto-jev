"""UI control requests: the dashboard only writes a request file; an executor with process rights runs it.

Two executors share this module:
- app.supervisor (Windows run.cmd / run-paper.cmd) owns both processes as children;
- `python -m app.control` (Linux) is a root oneshot unit started by a systemd path unit whenever a
  request appears. The dashboard unit stays sandboxed (read-only repo, no systemctl).
"""
from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from typing import Any
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from . import admin

ACTIONS = ('fetch', 'pull', 'switch', 'restart', 'reset')
WALLET = 2000
REQUEST_STALE_MS = 30_000


def control_dir(data_dir: Path) -> Path:
    return data_dir / 'supervisor'


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = path.stat() if path.exists() else path.parent.stat()
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.' + path.stem + '.',
                                     suffix='.tmp', delete=False) as handle:
        temp = Path(handle.name)
        json.dump(value, handle, indent=2)
        handle.write('\n')
    if os.name != 'nt':
        os.chmod(temp, stat.st_mode & 0o777 if path.exists() else 0o660)
        if os.geteuid() == 0:
            # The root helper must keep ownership, or the services lose write access to the file.
            os.chown(temp, stat.st_uid, stat.st_gid)
    os.replace(temp, path)


def request(data_dir: Path, action: str, branch: str | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(action)
    if action == 'switch' and not (branch and admin.BRANCH.fullmatch(branch) and '..' not in branch):
        raise ValueError(branch)
    request_id = secrets.token_hex(8)
    atomic_json(control_dir(data_dir) / 'request.json',
                {'id': request_id, 'action': action, 'branch': branch, 'requested_at': int(time.time()*1000)})
    return request_id


def pending(data_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((control_dir(data_dir) / 'request.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def take_request(data_dir: Path) -> dict[str, Any] | None:
    value = pending(data_dir)
    (control_dir(data_dir) / 'request.json').unlink(missing_ok=True)
    if not value or value.get('action') not in ACTIONS or not isinstance(value.get('id'), str):
        return None
    return value


def write_result(data_dir: Path, request: dict[str, Any], ok: bool, message: str, restarted: bool) -> None:
    atomic_json(control_dir(data_dir) / 'result.json',
                {'id': request['id'], 'action': request['action'], 'ok': ok, 'message': message[-2000:],
                 'restarted': restarted, 'finished_at': int(time.time()*1000)})


def read_result(data_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((control_dir(data_dir) / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def reset_paper(root: Path, data_dir: Path) -> Path | None:
    """Archive the Freqtrade trade database and restore the virtual wallet; executor must be stopped."""
    config_path = root / 'user_data' / 'config.paper.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('dry_run') is not True or config.get('strategy') != 'SignalBridgeStrategy':
        raise ValueError('Yalnız SignalBridgeStrategy dry-run konfiqurasiyası sıfırlana bilər.')
    db_url = config.get('db_url', '')
    if not db_url.startswith('sqlite:///'):
        raise ValueError('Yalnız lokal sqlite verilənlər bazası sıfırlana bilər.')
    database = (root / db_url.removeprefix('sqlite:///')).resolve()
    archive = None
    if database.exists():
        # Archived, never deleted: a reset can be undone by moving the files back.
        archive = data_dir / 'backups' / ('reset-' + time.strftime('%Y%m%d-%H%M%S'))
        archive.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt' and os.geteuid() == 0:
            owner = database.stat()
            for folder in (archive.parent, archive):
                os.chown(folder, owner.st_uid, owner.st_gid)
        for suffix in ('', '-wal', '-shm'):
            part = database.with_name(database.name + suffix)
            if part.exists():
                shutil.move(part, archive / part.name)
    if config.get('dry_run_wallet') != WALLET:
        config['dry_run_wallet'] = WALLET
        atomic_json(config_path, config)
    return archive


def perform(root: Path, data_dir: Path, request: dict[str, Any], stop: Callable[[], None]) -> tuple[str, bool]:
    """Run the git part and, when needed, stop the processes. Returns (message, restart needed).

    Git failures raise before anything is stopped, so a refused pull never interrupts trading.
    """
    action = request['action']
    if action == 'fetch':
        admin.fetch(root)
        return 'Uzaq repo yoxlandı.', False
    if action == 'pull':
        output = admin.pull(root)
        if 'Already up to date' in output:
            return 'Artıq aktualdır; restart lazım deyil.', False
    elif action == 'switch':
        output = admin.switch(root, str(request.get('branch') or ''))
    else:
        output = ''
    stop()
    if action == 'reset':
        archive = reset_paper(root, data_dir)
        output = f'Əməliyyatlar sıfırlandı, balans {WALLET} USDT.' + (f' Köhnə baza: {archive}' if archive else '')
    return (output or 'Restart edildi.'), True


def systemctl(*args: str) -> None:
    result = subprocess.run(['systemctl', *args], capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode('utf-8', 'replace').strip()[-1000:] or 'systemctl xətası')


def run_systemd(root: Path, data_dir: Path) -> int:
    dashboard = os.environ['CONTROL_DASHBOARD_SERVICE']
    freqtrade = os.environ['CONTROL_FREQTRADE_SERVICE']
    python = str(root / '.venv' / 'bin' / 'python')
    while request := take_request(data_dir):
        before = admin.head(root)
        try:
            message, restart = perform(root, data_dir, request, lambda: systemctl('stop', freqtrade, dashboard))
            if restart:
                notes = []
                if {'pyproject.toml', 'requirements.lock'} & set(admin.changed(root, before, admin.head(root))):
                    result = admin.run_as_owner(root, [python, '-m', 'pip', 'install', '-q', '-e', '.[paper]'], 900)
                    notes.append('Asılılıqlar yeniləndi.' if result.returncode == 0 else 'pip install uğursuz oldu; logu yoxlayın.')
                if (root / 'user_data' / 'config.paper.json').exists():
                    result = admin.run_as_owner(root, [python, '-m', 'app.paper', '--upgrade'], 120)
                    if result.returncode:
                        notes.append('Konfiqurasiya yenilənmədi: ' + result.stdout.decode('utf-8', 'replace')[-300:])
                systemctl('start', dashboard, freqtrade)
                message = ' '.join([message, *notes])
            write_result(data_dir, request, True, message, restart)
        except Exception as exc:
            # A failed stop/start must never leave the services down silently.
            subprocess.run(['systemctl', 'start', dashboard, freqtrade], capture_output=True, timeout=120)
            write_result(data_dir, request, False, str(exc) or type(exc).__name__, False)
        print(f'[control] {request["action"]} tamamlandı', flush=True)
    return 0


if __name__ == '__main__':
    from .config import Settings
    sys.exit(run_systemd(Path('.').resolve(), Settings.load().data_dir.resolve()))
