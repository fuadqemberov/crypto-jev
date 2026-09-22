import json
import sqlite3


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()

    def append(self, value):
        with self.db:
            self.db.execute('INSERT INTO history(payload) VALUES (?)', (json.dumps(value, allow_nan=False),))
            self.db.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 2000)')

    def history(self, limit=100):
        return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM history ORDER BY id DESC LIMIT ?', (limit,))]

    def close(self):
        self.db.close()

    def paused(self):
        row = self.db.execute("SELECT value FROM controls WHERE key='paused'").fetchone()
        return row is not None and row[0] == 'true'

    def set_paused(self, paused):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO controls VALUES ('paused', ?)", ('true' if paused else 'false',))
