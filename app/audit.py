"""Read-only, bounded decision/trade export. No credentials or upstream error bodies."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

ANALYSIS_FIELDS = ('symbol', 'observed_at', 'decision', 'rejection_codes', 'position_id',
                   'levels', 'frames', 'spread_bps', 'funding_rate', 'analysis_ms', 'lane')
TRADE_FIELDS = ('id', 'pair', 'is_open', 'is_short', 'leverage', 'open_rate', 'close_rate',
                'stake_amount', 'amount', 'close_profit_abs', 'open_date', 'close_date',
                'exit_reason', 'enter_tag', 'stop_loss', 'initial_stop_loss', 'funding_fees')


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def export(data_dir: Path, hours: int, limit: int = 10000) -> dict[str, Any]:
    if type(hours) is not int or not 1 <= hours <= 168 or not 1 <= limit <= 50000:
        raise ValueError('Invalid audit window/limit')
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    trades: list[dict[str, Any]] = []
    with closing(read_only(data_dir/'freqtrade-paper.sqlite')) as db:
        available = {row[1] for row in db.execute('PRAGMA table_info(trades)')}
        columns = [field for field in TRADE_FIELDS if field in available]
        if not {'id', 'pair', 'open_date'} <= set(columns):
            raise ValueError('Trade schema unavailable')
        db.row_factory = sqlite3.Row
        trades = [dict(row) for row in db.execute(
            f"SELECT {','.join(columns)} FROM trades WHERE open_date >= ? ORDER BY id DESC LIMIT ?",
            (since.replace(tzinfo=None).isoformat(sep=' '), limit))]
    symbols = {trade['pair'].replace('/USDT:USDT', 'USDT') for trade in trades}
    analyses = []
    with closing(read_only(data_dir/'analysis.db')) as db:
        for (payload,) in db.execute('SELECT payload FROM history ORDER BY id DESC LIMIT ?', (limit,)):
            row = json.loads(payload)
            if row.get('symbol') not in symbols or row.get('observed_at', 0) < since.timestamp()*1000:
                continue
            safe = {key: row[key] for key in ANALYSIS_FIELDS if key in row}
            ai = row.get('ai') or {}
            safe['ai'] = {'answers': {key: {field: value[field] for field in ('choice', 'confidence', 'probabilities') if field in value}
                for key, value in ai.get('answers', {}).items() if key in ('direction', 'momentum', 'regime', 'risk', 'driver', 'position_action')}}
            analyses.append(safe)
    return dict(generated_at=now.isoformat(), since=since.isoformat(), limit=limit,
                note='History reads the latest bounded rows; absence does not prove no decision existed.',
                trades=trades, analyses=analyses)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=int, default=6)
    parser.add_argument('--data-dir', type=Path, default=None)
    parser.add_argument('--output', type=Path, default=Path('data/jev-audit.json'))
    args = parser.parse_args()
    from .config import Settings
    data = export(args.data_dir or Settings.load().data_dir, args.hours)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f'Audit hazır: {args.output} ({len(data["trades"])} trade, {len(data["analyses"])} analiz)')


if __name__ == '__main__':
    main()
