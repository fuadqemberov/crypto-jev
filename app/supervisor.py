"""Local process supervisor: runs the dashboard and the Freqtrade paper executor as children.

The dashboard never manages processes itself; it drops a request file into the control directory
and this supervisor performs it. Restart and reset stop both children and exit with RESTART_CODE,
so the launcher script (run.cmd / run-paper.cmd) reinstalls, upgrades the config and starts again
with the freshly pulled code — including a new version of this supervisor.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

RESTART_CODE = 3
ACTIONS = ('restart', 'reset')
WALLET = 2000
ENV_FLAG = 'CRYPTO_RADAR_SUPERVISED'


def control_dir(data_dir: Path) -> Path:
    return data_dir / 'supervisor'


def request(data_dir: Path, action: str) -> None:
    """Called by the dashboard; atomic so the supervisor never reads a partial request."""
    if action not in ACTIONS:
        raise ValueError(action)
    folder = control_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, prefix='.request.', delete=False) as handle:
        temp = Path(handle.name)
        json.dump({'action': action, 'requested_at': int(time.time()*1000)}, handle)
    os.replace(temp, folder / 'request.json')


def take_request(data_dir: Path) -> str | None:
    path = control_dir(data_dir) / 'request.json'
    try:
        action = json.loads(path.read_text(encoding='utf-8')).get('action')
    except (OSError, ValueError, AttributeError):
        return None
    path.unlink(missing_ok=True)
    return action if action in ACTIONS else None


def reset_paper(root: Path, data_dir: Path) -> Path | None:
    """Archive the Freqtrade trade database and restore the virtual wallet; executor must be stopped."""
    config_path = root / 'user_data' / 'config.paper.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('dry_run') is not True or config.get('strategy') not in ('SignalBridgeStrategy', 'JevBridgeStrategy'):
        raise ValueError('Yalnız SignalBridgeStrategy dry-run konfiqurasiyası sıfırlana bilər.')
    db_url = config.get('db_url', '')
    if not db_url.startswith('sqlite:///'):
        raise ValueError('Yalnız lokal sqlite verilənlər bazası sıfırlana bilər.')
    database = (root / db_url.removeprefix('sqlite:///')).resolve()
    archive = None
    if database.exists():
        # Archived, never deleted: a reset can be undone by moving the files back.
        backups = data_dir / 'backups' / ('reset-' + time.strftime('%Y%m%d-%H%M%S'))
        backups.mkdir(parents=True, exist_ok=True)
        for suffix in ('', '-wal', '-shm'):
            part = database.with_name(database.name + suffix)
            if part.exists():
                shutil.move(part, backups / part.name)
        archive = backups
    if config.get('dry_run_wallet') != WALLET:
        config['dry_run_wallet'] = WALLET
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config_path.parent,
                                         prefix='.config.paper.', suffix='.tmp', delete=False) as handle:
            temp = Path(handle.name)
            json.dump(config, handle, indent=2)
            handle.write('\n')
        os.replace(temp, config_path)
    return archive


class Supervisor:
    def __init__(self, root: Path, data_dir: Path) -> None:
        self.root, self.data_dir = root, data_dir
        self.children: dict[str, subprocess.Popen[bytes]] = {}

    def commands(self) -> dict[str, list[str]]:
        python = sys.executable
        commands = {'dashboard': [python, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                  '--port', '8082', '--workers', '1']}
        if (self.root / 'user_data' / 'config.paper.json').exists() and importlib.util.find_spec('freqtrade'):
            commands['freqtrade'] = [python, '-m', 'freqtrade', 'trade', '--config', 'user_data/config.paper.json',
                                     '--strategy-path', 'user_data/strategies']
        return commands

    def spawn(self, name: str, command: list[str]) -> None:
        print(f'[supervisor] {name} başladılır', flush=True)
        # Own process group: console Ctrl+C reaches only the supervisor, which then stops children in order.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        self.children[name] = subprocess.Popen(command, cwd=self.root, env={**os.environ, ENV_FLAG: '1'}, creationflags=flags)

    def start(self) -> None:
        for name, command in self.commands().items():
            self.spawn(name, command)

    def stop(self) -> None:
        for name, child in self.children.items():
            if child.poll() is None:
                print(f'[supervisor] {name} dayandırılır', flush=True)
                stop_signal: Any = signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT
                try:
                    child.send_signal(stop_signal)
                except OSError:
                    pass
        deadline = time.monotonic() + 20
        for child in self.children.values():
            try:
                child.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.children.clear()

    def run(self) -> int:
        control_dir(self.data_dir).mkdir(parents=True, exist_ok=True)
        take_request(self.data_dir)  # A request left over from a previous run must not loop restarts.
        self.start()
        try:
            while True:
                time.sleep(1)
                action = take_request(self.data_dir)
                if action:
                    print(f'[supervisor] UI sorğusu: {action}', flush=True)
                    self.stop()
                    if action == 'reset':
                        archive = reset_paper(self.root, self.data_dir)
                        print(f'[supervisor] Əməliyyatlar sıfırlandı, balans {WALLET} USDT'
                              + (f'; köhnə baza: {archive}' if archive else ''), flush=True)
                    return RESTART_CODE
                for name, child in list(self.children.items()):
                    if child.poll() is not None:
                        # A crashed child is restarted alone; the other process keeps running.
                        print(f'[supervisor] {name} dayandı (kod {child.returncode}); 5 san sonra yenidən başladılır', flush=True)
                        time.sleep(5)
                        self.spawn(name, self.commands()[name])
        except KeyboardInterrupt:
            self.stop()
            return 0


if __name__ == '__main__':
    from dotenv import load_dotenv
    # Legacy Windows code pages cannot encode Azerbaijani letters; a log line must never kill the supervisor.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors='replace')  # type: ignore[union-attr]
    load_dotenv()
    sys.exit(Supervisor(Path('.').resolve(), Path(os.getenv('DATA_DIR', 'data')).resolve()).run())
