"""Export every strategy's results into one zip: python -m app.export

The zip (written to the home directory) contains a consistent copy of the database (futures.db),
XULASE.csv (ranking of all strategies) and strategiyalar/<key>/{emeliyyatlar,balans,aciq_movqeler}.csv.
Safe to run while the bot is running (SQLite online backup). CSVs open directly in Excel.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import sqlite3
import zipfile

from backtest.strategies import STRATEGIES

from .config import Settings


def main() -> str:
    names = {s.key: s.name for s in STRATEGIES}
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M')
    home = os.path.expanduser('~')
    src_path = os.environ.get('DB') or str(Settings.load().db_path)
    if not os.path.exists(src_path):
        raise SystemExit(f'Baza tapılmadı: {src_path} — layihə qovluğunda işlədin (cd ~/crypto-jev).')
    snap, out = f'{home}/futures-{stamp}.db', f'{home}/crypto-results-{stamp}.zip'
    src, dst = sqlite3.connect(src_path), sqlite3.connect(snap)
    src.backup(dst); dst.close(); src.close()                      # consistent copy while the bot keeps running
    db = sqlite3.connect(snap); db.row_factory = sqlite3.Row
    def utc(ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M') if ms else ''
    def table(rows, cols):
        f = io.StringIO(); w = csv.writer(f); w.writerow(cols); w.writerows(rows); return '\ufeff' + f.getvalue()   # BOM: Excel reads UTF-8 (ə, ı, ş)
    side = {1: 'LONG', -1: 'SHORT'}

    def voters(row):
        return (row['voters'] or '').replace(',', ' + ') if 'voters' in row.keys() else ''
    summary = []
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(snap, 'futures.db')
        for a in db.execute('SELECT * FROM accounts ORDER BY key'):
            k = a['key']
            tr = db.execute('SELECT * FROM trades WHERE key=? ORDER BY closed_at', (k,)).fetchall()
            z.writestr(f'strategiyalar/{k}/emeliyyatlar.csv', table(
                [[utc(t['opened_at']), utc(t['closed_at']), t['symbol'], side[t['side']], t['leverage'], t['entry_price'], t['exit_price'],
                  round(t['margin'], 4), round(t['pnl'], 4), round(t['ret'] * 100, 2), round(t['fees'], 4), round(t['funding'], 4), t['reason'], voters(t)] for t in tr],
                ['acilis_utc', 'baglanis_utc', 'coin', 'istiqamet', 'leverage', 'giris', 'cixis', 'marja', 'pnl_usdt', 'pnl_marja_%', 'komissiya', 'funding', 'sebeb', 'razilasanlar']))
            eq = db.execute('SELECT ts, equity FROM equity WHERE key=? ORDER BY ts', (k,)).fetchall()
            z.writestr(f'strategiyalar/{k}/balans.csv', table([[utc(r['ts']), round(r['equity'], 2)] for r in eq], ['vaxt_utc', 'balans']))
            pos = db.execute('SELECT * FROM positions WHERE key=?', (k,)).fetchall()
            z.writestr(f'strategiyalar/{k}/aciq_movqeler.csv', table(
                [[utc(p['opened_at']), p['symbol'], side[p['side']], p['leverage'], p['entry_price'], round(p['margin'], 4), p['stop_price'], p['tp_price'], voters(p)] for p in pos],
                ['acilis_utc', 'coin', 'istiqamet', 'leverage', 'giris', 'marja', 'stop', 'tp', 'razilasanlar']))
            pnl = [t['pnl'] for t in tr]
            win, loss = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
            last = eq[-1]['equity'] if eq else a['capital']
            reasons = [t['reason'] for t in tr]
            summary.append([k, names.get(k, k), a['capital'], round(last, 2), round((last / a['capital'] - 1) * 100, 2), len(tr),
                            round(sum(x > 0 for x in pnl) / len(pnl) * 100, 1) if pnl else '', round(win / loss, 2) if loss else '',
                            round(sum(pnl), 2), reasons.count('take_profit'), reasons.count('stop_loss'), reasons.count('liquidation'),
                            reasons.count('signal') + reasons.count('reverse'), len(pos)])
        summary.sort(key=lambda r: r[4], reverse=True)
        z.writestr('XULASE.csv', table([[i, *r] for i, r in enumerate(summary, 1)],
            ['yer', 'key', 'strategiya', 'baslangic', 'son_balans', 'gelir_%', 'emeliyyat', 'udus_%', 'menfeet_faktoru',
             'realize_pnl', 'tp', 'stop', 'likvidasiya', 'siqnal_baglanis', 'aciq_movqe']))
        bench = db.execute("SELECT ts, equity FROM equity WHERE key='_benchmark' ORDER BY ts").fetchall()
        z.writestr('benchmark_btc_balans.csv', table([[utc(r['ts']), round(r['equity'], 2)] for r in bench], ['vaxt_utc', 'balans']))
    db.close(); os.remove(snap)
    print(f'Hazirdir: {out}  ({os.path.getsize(out) / 1024:.0f} KB, {len(summary)} strategiya)')
    return out


if __name__ == '__main__':
    main()
