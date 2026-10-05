import json
import sqlite3
from datetime import datetime, timezone
import pytest
from app.audit import export


def test_audit_is_read_only_filters_traded_symbols_and_omits_secrets(tmp_path):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(tmp_path/'freqtrade-paper.sqlite') as db:
        db.execute('CREATE TABLE trades (id INTEGER, pair TEXT, open_date TEXT, private_key TEXT)')
        db.execute('INSERT INTO trades VALUES (1, ?, ?, ?)', ('APT/USDT:USDT', now.replace(tzinfo=None).isoformat(sep=' '), 'secret'))
    with sqlite3.connect(tmp_path/'analysis.db') as db:
        db.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, payload TEXT)')
        for symbol in ('APTUSDT', 'BTCUSDT'):
            db.execute('INSERT INTO history(payload) VALUES (?)', (json.dumps(dict(symbol=symbol,
                observed_at=now.timestamp()*1000, decision='LONG', api_key='secret',
                ai_error='secret', ai={'answers': {'direction': {'choice':'LONG', 'confidence':.95, 'body':'secret'}}})),))
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    result = export(tmp_path, 6)
    assert len(result['trades']) == len(result['analyses']) == 1
    assert result['analyses'][0]['ai']['answers']['direction']['confidence'] == .95
    assert 'secret' not in json.dumps(result)
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


def test_audit_missing_db_does_not_create_empty_history(tmp_path):
    with pytest.raises(sqlite3.OperationalError): export(tmp_path, 6)
    assert not list(tmp_path.iterdir())
