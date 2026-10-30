"""Run the 8 planned experiments on one download and zip the results.

    python -m backtest.experiments --days 365 --top 50

Each variant replays the live rules (backtest/futures_sim.py) with a FIXED margin per order (default
100 USDT = 5 % of 2000), so no account "runs dry" and the out-of-sample third is really traded:

    1 base          live rules                          5 base_4h        4h candles
    2 short         only SHORT entries                  6 short_4h       4h + SHORT
    3 stoptp        no signal exits (stop / TP only)    7 stoptp_4h      4h + stop / TP only
    4 short_stoptp  SHORT + stop / TP only              8 short_stoptp_4h  all three

A strategy PASSES a variant when, measured against the 2000 USDT capital:
Sharpe > 1, out-of-sample profit factor > 1.2, max drawdown < 20 %, >= 200 trades, >= 50 of them
out-of-sample. Output: reports/experiments/<variant>/*.csv, comparison.csv, passed.csv and a zip in ~.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import Settings

from .data import HOUR_MS, BinanceData
from .futures_sim import BAR_MS, liquid_symbols, metrics, out_of_sample, resample, simulate, top_by_volume, walk_forward
from .strategies import STRATEGIES

log = logging.getLogger('backtest.experiments')

VARIANTS = [
    # name, side, signal exits, timeframe
    ('base', 'both', 'all', '1h'),
    ('short', 'short', 'all', '1h'),
    ('stoptp', 'both', 'none', '1h'),
    ('short_stoptp', 'short', 'none', '1h'),
    ('base_4h', 'both', 'all', '4h'),
    ('short_4h', 'short', 'all', '4h'),
    ('stoptp_4h', 'both', 'none', '4h'),
    ('short_stoptp_4h', 'short', 'none', '4h'),
]
CRITERIA = dict(sharpe=1.0, oos_pf=1.2, max_dd_pct=20.0, trades=200, oos_trades=50)


def evaluate(results, oos_share: float, train_days: int, test_days: int, pick: int, bar_ms: int):
    n = len(next(iter(results.values())).equity)
    split = int(n * (1 - oos_share))
    oos = out_of_sample(results, split).set_index('key')
    rows = []
    for k, r in results.items():
        m = metrics(r.equity, r.trades, r.capital, r.fixed, r.bars_per_year)
        o = oos.loc[k]
        row = dict(key=k, name=r.name, **m, oos_return_pct=o.out_sample_pct, is_return_pct=o.in_sample_pct,
                   is_pf=o.is_pf, oos_pf=o.oos_pf, oos_trades=int(o.oos_trades))
        row['passed'] = bool(row['sharpe'] > CRITERIA['sharpe'] and row['oos_pf'] > CRITERIA['oos_pf']
                             and row['max_dd_pct'] < CRITERIA['max_dd_pct'] and row['trades'] >= CRITERIA['trades']
                             and row['oos_trades'] >= CRITERIA['oos_trades'])
        rows.append(row)
    summary = pd.DataFrame(rows).sort_values('return_pct', ascending=False).reset_index(drop=True)
    per_day = 24 * HOUR_MS // bar_ms
    wf = walk_forward(results, train_days * per_day, test_days * per_day, pick)
    rho = float(oos[['in_sample_pct', 'out_sample_pct']].corr(method='spearman').iloc[0, 1])
    return summary, oos.reset_index(), wf, rho


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='8 eksperiment: istiqamət × siqnal çıxışı × timeframe (canlı qaydalar)')
    p.add_argument('--days', type=int, default=365)
    p.add_argument('--end', type=int, default=None, help='dövrün sonu, epoch ms (default: son bağlanmış saat)')
    p.add_argument('--top', type=int, default=50)
    p.add_argument('--symbols', nargs='*')
    p.add_argument('--margin', type=float, default=100.0, help='hər order üçün sabit marja, USDT')
    p.add_argument('--oos', type=float, default=0.33)
    p.add_argument('--train-days', type=int, default=30)
    p.add_argument('--test-days', type=int, default=7)
    p.add_argument('--pick', type=int, default=5)
    p.add_argument('--only', nargs='*', help='yalnız bu variantlar (məs. base short_4h)')
    p.add_argument('--cache', default='data/klines')
    p.add_argument('--out', default='reports/experiments')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    base = replace(Settings.load(), timeframe='1h')
    end = a.end or int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - a.days * 24 * HOUR_MS
    source = BinanceData(a.cache)
    hourly, _ = source.load_all(start, end, 0.97, a.symbols or liquid_symbols(source, a.top))
    if not a.symbols:
        hourly = top_by_volume(hourly, a.top)
    if not hourly:
        raise SystemExit('Coin tapılmadı.')
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    frames = {tf: resample(hourly, BAR_MS[tf]) for tf in {v[3] for v in VARIANTS}}
    comparison, passed = [], []
    for name, side, exits, tf in VARIANTS:
        if a.only and name not in a.only:
            continue
        t0 = time.time()
        settings = replace(base, signal_exits=exits)
        results = simulate(STRATEGIES, frames[tf], settings, side=side, fixed_margin=a.margin, bar_ms=BAR_MS[tf])
        summary, oos, wf, rho = evaluate(results, a.oos, a.train_days, a.test_days, a.pick, BAR_MS[tf])
        d = out / name
        d.mkdir()
        summary.to_csv(d / 'summary.csv', index=False)
        oos.to_csv(d / 'out_of_sample.csv', index=False)
        wf.to_csv(d / 'walk_forward.csv', index=False)
        comp = lambda col: float((np.prod(1 + wf[col] / 100) - 1) * 100) if len(wf) else 0.0  # noqa: E731
        best = summary.iloc[0]
        comparison.append(dict(
            variant=name, side=side, signal_exits=exits, timeframe=tf,
            total_pnl=float(summary.final.sum() - len(summary) * base.capital),
            strategies_positive=int((summary.return_pct > 0).sum()), strategies_oos_positive=int((summary.oos_return_pct > 0).sum()),
            passed=int(summary.passed.sum()), best=best['name'], best_return_pct=float(best.return_pct),
            best_oos_pf=float(best.oos_pf), median_return_pct=float(summary.return_pct.median()),
            fees=float(summary.fees.sum()), rho=rho, wf_picked_pct=comp('picked_pct'), wf_all_pct=comp('all_pct'),
            seconds=round(time.time() - t0)))
        for _, r in summary[summary.passed].iterrows():
            passed.append(dict(variant=name, **r.to_dict()))
        c = comparison[-1]
        log.info('%-16s müsbət %2d/26, OOS müsbət %2d, keçən %d, ən yaxşı %s %+.1f%% (%ds)', name,
                 c['strategies_positive'], c['strategies_oos_positive'], c['passed'], c['best'][:28], c['best_return_pct'], c['seconds'])
    cmp_df = pd.DataFrame(comparison)
    cmp_df.to_csv(out / 'comparison.csv', index=False)
    pd.DataFrame(passed).to_csv(out / 'passed.csv', index=False)

    print(f'\n{len(hourly)} coin, {a.days} gün, sabit marja {a.margin:g} $/order, gəlir 2000 $-a görə. '
          f'Keçid şərti: Sharpe>1, OOS PF>1.2, maks. geriləmə<20%, ≥200 əməliyyat (≥50 OOS)\n')
    print(f"{'Variant':16} {'Cəmi PnL$':>10} {'Müsbət':>6} {'OOS+':>5} {'Keçən':>5} {'Median%':>8} {'ρ':>5} "
          f"{'WF qalib%':>9} {'WF hamı%':>8}  Ən yaxşı")
    for c in comparison:
        print(f"{c['variant']:16} {c['total_pnl']:10.0f} {c['strategies_positive']:6d} {c['strategies_oos_positive']:5d} "
              f"{c['passed']:5d} {c['median_return_pct']:8.1f} {c['rho']:+5.2f} {c['wf_picked_pct']:9.1f} {c['wf_all_pct']:8.1f}  "
              f"{c['best'][:30]} ({c['best_return_pct']:+.1f}%, OOS PF {c['best_oos_pf']:.2f})")
    if passed:
        print('\nŞərtləri keçənlər:')
        for r in passed:
            print(f"  {r['variant']:16} {r['name'][:38]:38} gəlir {r['return_pct']:+.1f}%  Sharpe {r['sharpe']:.2f}  "
                  f"OOS PF {r['oos_pf']:.2f}  maks. geriləmə {r['max_dd_pct']:.1f}%  {r['trades']} əməliyyat")
    else:
        print('\nHeç bir strategiya heç bir variantda şərtləri keçmədi.')
    zip_path = shutil.make_archive(str(Path.home() / f'experiments-{time.strftime("%Y%m%d-%H%M")}'), 'zip', out)
    print(f'\nZip: {zip_path}')


if __name__ == '__main__':
    main()
