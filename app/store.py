from __future__ import annotations
from pathlib import Path
from typing import Any
import json
import sqlite3


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=.2, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS ai_cache (key TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL, accessed INTEGER NOT NULL DEFAULT 0)')
        if 'accessed' not in {row[1] for row in self.db.execute('PRAGMA table_info(ai_cache)')}:
            self.db.execute('ALTER TABLE ai_cache ADD COLUMN accessed INTEGER NOT NULL DEFAULT 0')
        self.db.execute('CREATE INDEX IF NOT EXISTS ai_cache_access ON ai_cache(accessed)')
        self.db.commit()

    def append(self, value: dict[str, Any]) -> None:
        with self.db:
            self.db.execute('INSERT INTO history(payload) VALUES (?)', (json.dumps(value, allow_nan=False),))
            self.db.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 2000)')

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM history ORDER BY id DESC LIMIT ?', (limit,))]

    def close(self) -> None:
        self.db.close()

    def paused(self) -> bool:
        row = self.db.execute("SELECT value FROM controls WHERE key='paused'").fetchone()
        return row is not None and row[0] == 'true'

    def set_paused(self, paused: bool) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO controls VALUES ('paused', ?)", ('true' if paused else 'false',))

    def probe(self) -> None:
        # A real write verifies storage recovery; never clear the latch merely on a new scan.
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO controls VALUES ('health', 'ok')")

    def cache_get(self, key: str) -> tuple[float, dict[str, Any]] | None:
        row = self.db.execute('SELECT created, payload FROM ai_cache WHERE key=?', (key,)).fetchone()
        if row:
            self.cache_touch(key)
        return (row[0], json.loads(row[1])) if row else None

    def cache_put(self, key: str, value: tuple[float, dict[str, Any]], capacity: int, ttl: int) -> None:
        import time
        with self.db:
            self.db.execute('DELETE FROM ai_cache WHERE created <= ? OR created > ?', (time.time()-ttl, time.time()))
            self.db.execute('INSERT OR REPLACE INTO ai_cache VALUES (?, ?, ?, (SELECT COALESCE(MAX(accessed), 0)+1 FROM ai_cache))',
                            (key, value[0], json.dumps(value[1], allow_nan=False)))
            self.db.execute('DELETE FROM ai_cache WHERE key NOT IN (SELECT key FROM ai_cache ORDER BY accessed DESC LIMIT ?)', (capacity,))

    def cache_touch(self, key: str) -> None:
        with self.db:
            self.db.execute('UPDATE ai_cache SET accessed=(SELECT COALESCE(MAX(accessed), 0)+1 FROM ai_cache) WHERE key=?', (key,))
