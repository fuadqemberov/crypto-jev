"""CLI: python -m backtest --days 183 --capital 2000 --out report.html"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from .data import HOUR_MS, BinanceData
from .engine import run_all
from .report import build_report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='Top 5 strategiyalı kripto backtest (1h, Binance spot USDT)')
    p.add_argument('--days', type=int, default=183, help='geriyə test müddəti, gün (default: 183 ≈ 6 ay)')
    p.add_argument('--end', type=int, default=None, help='dövrün sonu, epoch ms (default: son bağlanmış saat)')
    p.add_argument('--capital', type=float, default=2000.0, help='hər strategiya üçün başlanğıc balans, USDT')
    p.add_argument('--fee', type=float, default=0.10, help='komissiya, %% / tərəf')
    p.add_argument('--slippage', type=float, default=0.05, help='sürüşmə, %% / tərəf')
    p.add_argument('--symbols', nargs='*', help='məhdud coin siyahısı (məs. BTCUSDT ETHUSDT); default: hamısı')
    p.add_argument('--min-coverage', type=float, default=0.97, help='coin üçün minimal tarixçə payı')
    p.add_argument('--cache', default='data/klines', help='şam keşi qovluğu')
    p.add_argument('--out', default='crypto_strategies_report.html', help='HTML hesabat faylı')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    end = a.end or int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - a.days * 24 * HOUR_MS
    data, skipped = BinanceData(a.cache).load_all(start, end, a.min_coverage, a.symbols)
    cost = (a.fee + a.slippage) / 100
    results, bench = run_all(data, a.capital, cost)

    print(f'\n{len(data)} coin, {len(results)} strategiya, {a.days} gün, 1h\n')
    print(f"{'#':>2} {'Strategiya':34} {'Gəlir%':>8} {'Balans$':>9} {'Uduş%':>6} {'MaxDD%':>7} {'Sharpe':>6} {'Əməl.':>6} {'PF':>5}")
    for i, r in enumerate(results + [bench], 1):
        m = r.metrics
        print(f"{i if r is not bench else '-':>2} {r.strategy.name:34} {m['total_return']:8.2f} {m['final_balance']:9.2f} "
              f"{m['win_rate']:6.1f} {m['max_drawdown']:7.2f} {m['sharpe']:6.2f} {m['trades']:6d} {m['profit_factor']:5.2f}")
    out = build_report(results, bench, data, skipped, Path(a.out), a.capital, cost)
    print(f'\nHesabat: {out.resolve()}')


if __name__ == '__main__':
    main()
