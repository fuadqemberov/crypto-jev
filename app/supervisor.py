"""Local process supervisor: runs the dashboard and the Freqtrade paper executor as children.

The dashboard never manages processes itself; it drops a request file (app.control) and this
supervisor performs it. Restart, reset, pull and switch stop both children and exit with RESTART_CODE,
so the launcher script (run.cmd / run-paper.cmd) reinstalls, upgrades the config and starts again
with the freshly pulled code — including a new version of this supervisor.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
import importlib.util
import os
import signal
import subprocess
import sys
import time

from . import control

RESTART_CODE = 3
ENV_FLAG = 'CRYPTO_RADAR_SUPERVISED'


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
        control.take_request(self.data_dir)  # A request left over from a previous run must not loop restarts.
        self.start()
        try:
            while True:
                time.sleep(1)
                request = control.take_request(self.data_dir)
                if request:
                    print(f'[supervisor] UI sorğusu: {request["action"]}', flush=True)
                    try:
                        message, restart = control.perform(self.root, self.data_dir, request, self.stop)
                    except Exception as exc:
                        control.write_result(self.data_dir, request, False, str(exc) or type(exc).__name__, False)
                        if not self.children:
                            return RESTART_CODE  # Stopped before failing (reset): start again rather than stay down.
                        continue
                    print(f'[supervisor] {message}', flush=True)
                    control.write_result(self.data_dir, request, True, message, restart)
                    if restart:
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
