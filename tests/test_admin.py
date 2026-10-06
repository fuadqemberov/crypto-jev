import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import admin, control, supervisor
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
    request_id = control.request(tmp_path, 'reset')
    assert control.take_request(tmp_path)['id'] == request_id
    assert control.take_request(tmp_path) is None
    for action, branch in (('rm -rf', None), ('switch', '--orphan'), ('switch', '../x')):
        with pytest.raises(ValueError):
            control.request(tmp_path, action, branch)


def test_reset_archives_trades_and_restores_wallet(tmp_path):
    root = paper_root(tmp_path)
    archive = control.reset_paper(root, root / 'data')
    assert not list((root / 'data').glob('freqtrade-paper.sqlite*'))
    assert sorted(p.name for p in archive.iterdir()) == ['freqtrade-paper.sqlite', 'freqtrade-paper.sqlite-shm', 'freqtrade-paper.sqlite-wal']
    assert json.loads((root / 'user_data' / 'config.paper.json').read_text(encoding='utf-8'))['dry_run_wallet'] == 2000


def test_reset_refuses_live_config(tmp_path):
    root = paper_root(tmp_path)
    path = root / 'user_data' / 'config.paper.json'
    path.write_text(json.dumps({**json.loads(path.read_text(encoding='utf-8')), 'dry_run': False}), encoding='utf-8')
    with pytest.raises(ValueError):
        control.reset_paper(root, root / 'data')
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
    return origin, clone


def test_git_pull_and_switch(repos):
    origin, clone = repos
    (origin / 'a.txt').write_text('2')
    run(origin, 'commit', '-qam', 'two')
    admin.fetch(clone)
    info = admin.info(clone)
    assert info['branch'] == 'master' and info['behind'] == 1 and 'feature' in info['branches']
    before = admin.head(clone)
    admin.pull(clone)
    assert (clone / 'a.txt').read_text() == '2' and admin.changed(clone, before, admin.head(clone)) == ['a.txt']
    admin.switch(clone, 'feature')
    assert admin.info(clone)['branch'] == 'feature'


def test_git_refuses_dirty_tree_and_unknown_branch(repos):
    _, clone = repos
    for name in ('--orphan', 'nope', '../x'):
        with pytest.raises(admin.GitError):
            admin.switch(clone, name)
    (clone / 'a.txt').write_text('local edit')
    with pytest.raises(admin.GitError):
        admin.pull(clone)
    with pytest.raises(admin.GitError):
        admin.switch(clone, 'feature')


def test_perform_stops_only_after_git_succeeds(repos):
    _, clone = repos
    stops = []
    assert control.perform(clone, clone, {'id': 'a', 'action': 'pull'}, lambda: stops.append(1)) == ('Artıq aktualdır; restart lazım deyil.', False)
    (clone / 'a.txt').write_text('local edit')
    with pytest.raises(admin.GitError):
        control.perform(clone, clone, {'id': 'b', 'action': 'switch', 'branch': 'feature'}, lambda: stops.append(1))
    assert stops == []
    assert control.perform(clone, clone, {'id': 'c', 'action': 'restart'}, lambda: stops.append(1))[1] is True
    assert stops == [1]


def test_admin_api_requires_executor_and_writes_request(tmp_path, monkeypatch):
    monkeypatch.delenv(supervisor.ENV_FLAG, raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path), start_worker=False)) as client:
        assert client.post('/api/admin/restart').status_code == 403
        assert client.post('/api/admin/restart', headers=HEADERS).status_code == 409
        assert client.get('/api/admin/info').json()['control'] is None
        # The systemd marker is written by deploy/install-control.sh.
        (tmp_path / 'supervisor').mkdir()
        (tmp_path / 'supervisor' / 'systemd.json').write_text('{}')
        assert client.get('/api/admin/info').json()['control'] == 'systemd'
        assert client.post('/api/admin/other', headers=HEADERS).status_code == 404
        assert client.post('/api/admin/switch', headers=HEADERS, json={'branch': '-x'}).status_code == 422
        request_id = client.post('/api/admin/reset', headers=HEADERS).json()['id']
        assert client.post('/api/admin/restart', headers=HEADERS).status_code == 409
        assert client.get('/api/admin/info').json()['pending']['id'] == request_id
        control.write_result(tmp_path, control.take_request(tmp_path), True, 'ok', True)
        assert client.get('/api/admin/info').json()['result']['id'] == request_id
    monkeypatch.setenv(supervisor.ENV_FLAG, '1')
    with TestClient(create_app(Settings(data_dir=tmp_path / 'other'), start_worker=False)) as client:
        assert client.get('/api/admin/info').json()['control'] == 'supervisor'
