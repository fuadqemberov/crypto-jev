import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import admin, supervisor
from app.config import Settings
from app.main import create_app

HEADERS = {'X-Crypto-Radar': '1'}


def paper_root(tmp_path: Path, wallet: int = 500) -> Path:
    (tmp_path / 'user_data').mkdir()
    (tmp_path / 'data').mkdir()
    config = {'dry_run': True, 'strategy': 'SignalBridgeStrategy', 'dry_run_wallet': wallet,
              'db_url': 'sqlite:///data/freqtrade-paper.sqlite'}
    (tmp_path / 'user_data' / 'config.paper.json').write_text(json.dumps(config), encoding='utf-8')
    for suffix in ('', '-wal', '-shm'):
        (tmp_path / 'data' / f'freqtrade-paper.sqlite{suffix}').write_text('trades', encoding='utf-8')
    return tmp_path


def test_request_is_consumed_once(tmp_path):
    supervisor.request(tmp_path, 'reset')
    assert supervisor.take_request(tmp_path) == 'reset'
    assert supervisor.take_request(tmp_path) is None
    with pytest.raises(ValueError):
        supervisor.request(tmp_path, 'rm -rf')


def test_reset_archives_trades_and_restores_wallet(tmp_path):
    root = paper_root(tmp_path)
    archive = supervisor.reset_paper(root, root / 'data')
    assert not list((root / 'data').glob('freqtrade-paper.sqlite*'))
    assert sorted(p.name for p in archive.iterdir()) == ['freqtrade-paper.sqlite', 'freqtrade-paper.sqlite-shm', 'freqtrade-paper.sqlite-wal']
    assert json.loads((root / 'user_data' / 'config.paper.json').read_text(encoding='utf-8'))['dry_run_wallet'] == 2000


def test_reset_refuses_live_config(tmp_path):
    root = paper_root(tmp_path)
    path = root / 'user_data' / 'config.paper.json'
    path.write_text(json.dumps({**json.loads(path.read_text(encoding='utf-8')), 'dry_run': False}), encoding='utf-8')
    with pytest.raises(ValueError):
        supervisor.reset_paper(root, root / 'data')
    assert (root / 'data' / 'freqtrade-paper.sqlite').exists()


def run(cwd: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path):
    origin, clone = tmp_path / 'origin', tmp_path / 'clone'
    origin.mkdir()
    run(origin, 'init', '-q', '-b', 'master')
    for args in (('config', 'user.email', 't@t'), ('config', 'user.name', 't')):
        run(origin, *args)
    (origin / 'a.txt').write_text('1')
    run(origin, 'add', '.'); run(origin, 'commit', '-qm', 'one')
    run(origin, 'branch', 'feature')
    run(tmp_path, 'clone', '-q', str(origin), str(clone))
    for args in (('config', 'user.email', 't@t'), ('config', 'user.name', 't')):
        run(clone, *args)
    return origin, clone


def test_git_pull_and_switch(repos):
    origin, clone = repos
    (origin / 'a.txt').write_text('2')
    run(origin, 'commit', '-qam', 'two')
    info = asyncio.run(admin.info(clone, fetch=True))
    assert info['branch'] == 'master' and info['behind'] == 1 and 'feature' in info['branches']
    asyncio.run(admin.pull(clone))
    assert (clone / 'a.txt').read_text() == '2'
    asyncio.run(admin.switch(clone, 'feature'))
    assert asyncio.run(admin.info(clone))['branch'] == 'feature'


def test_git_refuses_dirty_tree_and_unknown_branch(repos):
    _, clone = repos
    for name in ('--orphan', 'nope', '../x'):
        with pytest.raises(admin.GitError):
            asyncio.run(admin.switch(clone, name))
    (clone / 'a.txt').write_text('local edit')
    with pytest.raises(admin.GitError):
        asyncio.run(admin.pull(clone))
    with pytest.raises(admin.GitError):
        asyncio.run(admin.switch(clone, 'feature'))


def test_admin_actions_need_supervisor_and_header(tmp_path, monkeypatch):
    monkeypatch.delenv(supervisor.ENV_FLAG, raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path), start_worker=False)) as client:
        assert client.post('/api/admin/restart').status_code == 403
        assert client.post('/api/admin/restart', headers=HEADERS).status_code == 409
        assert client.get('/api/admin/info').json()['supervised'] is False
        monkeypatch.setenv(supervisor.ENV_FLAG, '1')
        assert client.post('/api/admin/other', headers=HEADERS).status_code == 404
        assert client.post('/api/admin/reset', headers=HEADERS).status_code == 202
    assert supervisor.take_request(tmp_path) == 'reset'
