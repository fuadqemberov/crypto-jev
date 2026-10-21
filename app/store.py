"""SQLite persistence for the virtual futures accounts (positions, trades, equity curve)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    key TEXT PRIMARY KEY, cash REAL NOT NULL, capital REAL NOT NULL, started_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    key TEXT NOT NULL, symbol TEXT NOT NULL, side INTEGER NOT NULL, leverage INTEGER NOT NULL,
    qty REAL NOT NULL, entry_price REAL NOT NULL, fill_price REAL NOT NULL, margin REAL NOT NULL,
    fee REAL NOT NULL, funding REAL NOT NULL, opened_at INTEGER NOT NULL, stop_price REAL NOT NULL DEFAULT 0,
    tp_price REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (key, symbol));
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, symbol TEXT NOT NULL, side INTEGER NOT NULL,
    leverage INTEGER NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
    margin REAL NOT NULL, notional REAL NOT NULL, proceeds REAL NOT NULL, pnl REAL NOT NULL, ret REAL NOT NULL,
    fees REAL NOT NULL, funding REAL NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL,
    reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS trades_key ON trades (key, closed_at);
CREATE TABLE IF NOT EXISTS equity (
    key TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, PRIMARY KEY (key, ts));
"""

POSITION_COLUMNS = ('symbol', 'side', 'leverage', 'qty', 'entry_price', 'fill_price', 'margin', 'fee', 'funding', 'opened_at',
                    'stop_price', 'tp_price')
TRADE_COLUMNS = ('symbol', 'side', 'leverage', 'qty', 'entry_price', 'exit_price', 'margin', 'notional', 'proceeds',
                 'pnl', 'ret', 'fees', 'funding', 'opened_at', 'closed_at', 'reason')


class Store:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ':memory:':
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)
        cols = {r['name'] for r in self.db.execute('PRAGMA table_info(positions)')}
        for col in ('stop_price', 'tp_price'):            # databases created before stop-loss / R take-profit
            if col not in cols:
                self.db.execute(f'ALTER TABLE positions ADD COLUMN {col} REAL NOT NULL DEFAULT 0')
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- meta ---------------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute('INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, json.dumps(value)))

    # -- accounts -----------------------------------------------------------------
    def accounts(self) -> dict[str, sqlite3.Row]:
        return {r['key']: r for r in self.db.execute('SELECT * FROM accounts')}

    def create_account(self, key: str, capital: float, started_at: int) -> None:
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO accounts(key, cash, capital, started_at) VALUES (?, ?, ?, ?)',
                            (key, capital, capital, started_at))

    def positions(self) -> list[sqlite3.Row]:
        return self.db.execute('SELECT * FROM positions ORDER BY opened_at').fetchall()

    def open_position(self, key: str, cash: float, p: dict) -> None:
        with self.db:
            self.db.execute('UPDATE accounts SET cash=? WHERE key=?', (cash, key))
            self.db.execute(f'INSERT INTO positions(key, {", ".join(POSITION_COLUMNS)}) '
                            f'VALUES (:key, {", ".join(":" + c for c in POSITION_COLUMNS)})',
                            {'key': key, **{c: p[c] for c in POSITION_COLUMNS}})

    def update_margin(self, rows: list[tuple[float, float, str, str]]) -> None:
        """(margin, funding, key, symbol) after a funding settlement."""
        with self.db:
            self.db.executemany('UPDATE positions SET margin=?, funding=? WHERE key=? AND symbol=?', rows)

    def close_position(self, key: str, cash: float, t: dict) -> int:
        with self.db:
            self.db.execute('UPDATE accounts SET cash=? WHERE key=?', (cash, key))
            self.db.execute('DELETE FROM positions WHERE key=? AND symbol=?', (key, t['symbol']))
            cur = self.db.execute(f'INSERT INTO trades(key, {", ".join(TRADE_COLUMNS)}) '
                                  f'VALUES (:key, {", ".join(":" + c for c in TRADE_COLUMNS)})',
                                  {'key': key, **{c: t[c] for c in TRADE_COLUMNS}})
            return int(cur.lastrowid)

    # -- trades / equity ------------------------------------------------------------
    def trades(self, key: str, limit: int = 500) -> list[dict]:
        rows = self.db.execute('SELECT * FROM trades WHERE key=? ORDER BY closed_at DESC, id DESC LIMIT ?', (key, limit))
        return [dict(r) for r in rows]

    def trade_stats(self) -> dict[str, dict]:
        rows = self.db.execute("""
            SELECT key, COUNT(*) n, SUM(pnl) pnl, SUM(fees) fees, SUM(funding) funding, SUM(pnl > 0) wins,
                   SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) gross_win,
                   -SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) gross_loss,
                   AVG(ret) avg_ret, MAX(ret) best, MIN(ret) worst, AVG(closed_at - opened_at) avg_hold,
                   SUM(reason = 'take_profit') tp, SUM(reason = 'liquidation') liq, SUM(reason = 'stop_loss') sl, SUM(reason = 'timeout') tmo,
                   SUM(side = 1) longs, SUM(side = -1) shorts, AVG(leverage) avg_lev
            FROM trades GROUP BY key""")
        return {r['key']: dict(r) for r in rows}

    def last_closes(self) -> dict[tuple[str, str], int]:
        rows = self.db.execute('SELECT key, symbol, MAX(closed_at) t FROM trades GROUP BY key, symbol')
        return {(r['key'], r['symbol']): int(r['t']) for r in rows}

    def add_equity(self, ts: int, values: dict[str, float]) -> None:
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO equity(key, ts, equity) VALUES (?, ?, ?)',
                                [(k, ts, v) for k, v in values.items()])

    def equity(self, key: str) -> list[tuple[int, float]]:
        return [(r['ts'], r['equity']) for r in self.db.execute('SELECT ts, equity FROM equity WHERE key=? ORDER BY ts', (key,))]

    def equity_peaks(self) -> dict[str, tuple[float, float]]:
        """Max drawdown per account from the stored curve: key -> (peak, max_dd fraction)."""
        out: dict[str, tuple[float, float]] = {}
        for r in self.db.execute('SELECT key, equity FROM equity ORDER BY key, ts'):
            peak, dd = out.get(r['key'], (0.0, 0.0))
            peak = max(peak, r['equity'])
            dd = max(dd, 1 - r['equity'] / peak if peak > 0 else 0.0)
            out[r['key']] = (peak, dd)
        return out
