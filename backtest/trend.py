"""Stage 2: a diversified trend portfolio — the one approach that held up after costs in published studies.

    python -m backtest.trend --days 730 --top 20

Not a per-trade signal bot but one portfolio account:

* universe   the ``--top`` most liquid coins by trailing 30-day turnover, re-chosen every 30 days;
* signal     an ensemble of Donchian breakouts (10 / 20 / 40 / 80 days): +1 after a close above the
             N-day high, -1 after a close below the N-day low, flat again after a close beyond the
             N/2-day opposite band; the coin's signal is the mean over the lookbacks (-1 … +1);
* sizing     volatility targeting: weight = signal × (target_vol / √k) / coin_vol (30-day realised),
             each coin capped at ``--max-weight`` and the whole book at ``--max-gross`` × equity;
* trading    weights decided at a bar close are held over the next bar; a coin is only re-traded when
             its target moved by more than ``--band`` of equity; fee + slippage on every change;
             longs pay a constant funding of 0.01 % / 8 h (shorts get nothing — conservative).

Four variants are fixed in advance (long+short / long-only × 1d / 6h) so we don't tune on the
result, and judged with the same bar as the signal strategies, measured on the compounding equity:
Sharpe > 1, last-third (out-of-sample) Sharpe > 0.5 with a positive return, max drawdown < 20 %,
>= 200 fills. Benchmarks: holding BTC, and holding the same universe equally weighted.

Caveat: the candidate coins are today's most liquid ones that already existed at the start
(survivorship bias) — a pass here is necessary, not sufficient.
"""
from __future__ import annotations

import argparse
import logging
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .data import HOUR_MS, BinanceData
from .futures_sim import BAR_MS, liquid_symbols, resample

log = logging.getLogger('backtest.trend')
DAY_MS = 24 * HOUR_MS
VARIANTS = [
    # name, long_only, timeframe
    ('long_short_1d', False, '1d'),
    ('long_only_1d', True, '1d'),
    ('long_short_6h', False, '6h'),
    ('long_only_6h', True, '6h'),
]
CRITERIA = dict(sharpe=1.0, oos_sharpe=0.5, max_dd_pct=20.0, fills=200)


@dataclass
class TrendParams:
    lookbacks_days: tuple[int, ...] = (10, 20, 40, 80)
    vol_days: int = 30
    target_vol: float = 0.25          # annualised portfolio volatility the sizing aims for
    max_weight: float = 0.5           # |weight| cap per coin, × equity
    max_gross: float = 2.0            # sum |weight| cap, × equity
    band: float = 0.02                # re-trade a coin only when its target moved by more than this
    top: int = 20
    rotate_days: int = 30
    turnover_days: int = 30
    long_only: bool = False
    cost_rate: float = 0.001          # fee + slippage per side (0.05 % + 0.05 %, like the live engine)
    funding_8h: float = 0.0001
    capital: float = 2000.0


@dataclass
class TrendResult:
    name: str
    times: np.ndarray                 # bar open times
    equity: np.ndarray                # equity at every bar close (first value: after bar 0)
    weights: np.ndarray               # held weights over each bar, shape (T, N)
    symbols: list[str]
    fills: int
    fees: float
    funding: float
    turnover: float                   # sum |Δw| (× equity)
    bars_per_year: float
    start: int                        # first evaluated bar (after warm-up)


# --- signal ----------------------------------------------------------------------------------
def donchian_state(close: np.ndarray, high: np.ndarray, low: np.ndarray, n: int) -> np.ndarray:
    """+1 / 0 / -1 per bar for one coin and one lookback; only bars <= t are used at t."""
    h = pd.Series(high).rolling(n, min_periods=n).max().shift().to_numpy()
    lo = pd.Series(low).rolling(n, min_periods=n).min().shift().to_numpy()
    m = max(n // 2, 1)
    hx = pd.Series(high).rolling(m, min_periods=m).max().shift().to_numpy()
    lx = pd.Series(low).rolling(m, min_periods=m).min().shift().to_numpy()
    out = np.zeros(len(close))
    s = 0
    for t in range(len(close)):
        if np.isnan(h[t]):
            continue
        if s == 1 and close[t] < lx[t]:
            s = 0
        elif s == -1 and close[t] > hx[t]:
            s = 0
        if close[t] > h[t]:
            s = 1
        elif close[t] < lo[t]:
            s = -1
        out[t] = s
    return out


def target_weights(data: dict[str, pd.DataFrame], p: TrendParams, bar_ms: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """(weights[T, N], close[T, N], symbols): the weight decided at the close of every bar."""
    symbols = list(data)
    per_day = DAY_MS // bar_ms
    close = np.column_stack([data[s].close.to_numpy(float) for s in symbols])
    high = np.column_stack([data[s].high.to_numpy(float) for s in symbols])
    low = np.column_stack([data[s].low.to_numpy(float) for s in symbols])
    turnover = np.column_stack([(data[s].close * data[s].volume).to_numpy(float) for s in symbols])
    T, N = close.shape
    sig = np.zeros((T, N))
    for j in range(N):
        sig[:, j] = np.mean([donchian_state(close[:, j], high[:, j], low[:, j], d * per_day)
                             for d in p.lookbacks_days], axis=0)
    if p.long_only:
        sig = np.clip(sig, 0, None)
    rets = np.vstack([np.zeros(N), close[1:] / close[:-1] - 1])
    bpy = 365 * per_day
    vol = pd.DataFrame(rets).rolling(p.vol_days * per_day, min_periods=p.vol_days * per_day).std().to_numpy() * math.sqrt(bpy)
    # universe: top-k by trailing turnover, re-chosen every rotate_days
    tv = pd.DataFrame(turnover).rolling(p.turnover_days * per_day, min_periods=p.turnover_days * per_day).mean().to_numpy()
    member = np.zeros((T, N), dtype=bool)
    current = np.zeros(N, dtype=bool)
    step = p.rotate_days * per_day
    for t in range(T):
        if not np.isnan(tv[t]).all() and (t % step == 0 or not current.any()):
            current = np.zeros(N, dtype=bool)
            order = np.argsort(-np.nan_to_num(tv[t], nan=-1))[:p.top]
            current[order[~np.isnan(tv[t][order])]] = True
        member[t] = current
    k = max(min(p.top, N), 1)
    with np.errstate(divide='ignore', invalid='ignore'):
        w = sig * (p.target_vol / math.sqrt(k)) / vol
    w = np.where(member & np.isfinite(w), w, 0.0)
    w = np.clip(w, -p.max_weight, p.max_weight)
    gross = np.abs(w).sum(axis=1, keepdims=True)
    w = np.where(gross > p.max_gross, w * p.max_gross / np.where(gross > 0, gross, 1), w)
    return w, close, symbols


def warmup_bars(p: TrendParams, bar_ms: int) -> int:
    per_day = DAY_MS // bar_ms
    return (max(max(p.lookbacks_days), p.vol_days, p.turnover_days) + 1) * per_day


def run(data: dict[str, pd.DataFrame], p: TrendParams, bar_ms: int, name: str = 'trend', start: int | None = None) -> TrendResult:
    """Trade the target weights; bars before ``start`` (default: the warm-up) are not traded."""
    target, close, symbols = target_weights(data, p, bar_ms)
    T, N = close.shape
    start = warmup_bars(p, bar_ms) if start is None else start
    if start >= T - 1:
        raise ValueError(f'{T} bar warm-up üçün azdır ({start} lazımdır)')
    rets = np.vstack([np.zeros(N), close[1:] / close[:-1] - 1])
    funding_bar = p.funding_8h * (bar_ms / HOUR_MS) / 8
    eq = p.capital
    held = np.zeros(N)
    equity = np.empty(T - start)
    weights = np.zeros((T - start, N))
    fills, fees, funding, turnover = 0, 0.0, 0.0, 0.0
    for t in range(start, T):
        if t > start:                                   # P&L of the weights held over bar t
            pnl = eq * float(held @ rets[t])
            fund = eq * funding_bar * float(np.clip(held, 0, None).sum())
            eq += pnl - fund
            funding += fund
            # positions drift with their own coin; re-express as weights of the new equity
            if eq > 0:
                held = held * (1 + rets[t]) * (eq + fund - pnl) / eq
            else:
                eq, held = 0.0, np.zeros(N)
        weights[t - start] = held
        # rebalance at the close of bar t toward the target decided with data <= t
        change = target[t] - held
        trade = (np.abs(change) > p.band) | ((target[t] == 0) & (held != 0))
        if eq > 0 and trade.any():
            dw = np.abs(change[trade]).sum()
            cost = eq * p.cost_rate * dw
            eq -= cost
            fees += cost
            turnover += dw
            fills += int(trade.sum())
            held = np.where(trade, target[t], held)
        equity[t - start] = eq
    times = next(iter(data.values())).index.to_numpy('int64')[start:]
    return TrendResult(name, times, equity, weights, symbols, fills, fees, funding, turnover, 365 * DAY_MS / bar_ms, start)


# --- evaluation ------------------------------------------------------------------------------
def _sharpe(eq: np.ndarray, bpy: float) -> float:
    r = np.diff(eq) / eq[:-1]
    return float(r.mean() / r.std() * math.sqrt(bpy)) if len(r) > 1 and r.std() > 0 else 0.0


def _max_dd(eq: np.ndarray) -> float:
    return float((1 - eq / np.maximum.accumulate(eq)).max() * 100)


def evaluate(res: TrendResult, capital: float, oos_share: float) -> dict:
    eq = np.concatenate([[capital], res.equity])
    split = int(len(eq) * (1 - oos_share))
    years = (len(eq) - 1) / res.bars_per_year
    ret = eq[-1] / capital - 1
    row = dict(name=res.name, final=float(eq[-1]), return_pct=float(ret * 100),
               cagr_pct=float(((eq[-1] / capital) ** (1 / years) - 1) * 100) if eq[-1] > 0 and years > 0 else -100.0,
               sharpe=_sharpe(eq, res.bars_per_year), max_dd_pct=_max_dd(eq),
               is_return_pct=float((eq[split] / capital - 1) * 100), is_sharpe=_sharpe(eq[:split + 1], res.bars_per_year),
               oos_return_pct=float((eq[-1] / eq[split] - 1) * 100), oos_sharpe=_sharpe(eq[split:], res.bars_per_year),
               oos_max_dd_pct=_max_dd(eq[split:]), fills=res.fills, fees=res.fees, funding=res.funding,
               turnover_x=res.turnover, avg_gross=float(np.abs(res.weights).sum(axis=1).mean()),
               avg_net=float(res.weights.sum(axis=1).mean()), time_in_market_pct=float((np.abs(res.weights).sum(axis=1) > 0).mean() * 100))
    row['passed'] = bool(row['sharpe'] > CRITERIA['sharpe'] and row['oos_sharpe'] > CRITERIA['oos_sharpe']
                         and row['oos_return_pct'] > 0 and row['max_dd_pct'] < CRITERIA['max_dd_pct']
                         and row['fills'] >= CRITERIA['fills'])
    return row


def benchmarks(data: dict[str, pd.DataFrame], res: TrendResult, capital: float, oos_share: float, top: int) -> list[dict]:
    """Buy-and-hold BTC and an equal-weight hold of the ``top`` most liquid coins, same period, one round-trip fee."""
    t0 = int(res.times[0])
    out = []
    frames = {s: df.loc[df.index >= t0] for s, df in data.items()}
    tv = {s: float((df.close * df.volume).iloc[:len(df) // 10 or 1].mean()) for s, df in frames.items()}
    picks = {'BTC al-saxla': ['BTCUSDT'] if 'BTCUSDT' in frames else [],
             f'Top-{top} bərabər al-saxla': sorted(tv, key=tv.get, reverse=True)[:top]}
    for name, syms in picks.items():
        if not syms:
            continue
        rel = np.mean([frames[s].close.to_numpy(float) / frames[s].close.iloc[0] for s in syms], axis=0)
        eq = capital * (1 - 0.001) * rel
        eq[-1] *= 1 - 0.001
        r = TrendResult(name, res.times, eq, np.ones((len(eq), 1)), syms, 2 * len(syms), 0, 0, 0, res.bars_per_year, 0)
        row = evaluate(r, capital, oos_share)
        row.update(fills=0, passed=False, avg_gross=1.0, avg_net=1.0, time_in_market_pct=100.0)
        out.append(row)
    return out


def quarterly(res: TrendResult, capital: float) -> pd.Series:
    eq = pd.Series(np.concatenate([[capital], res.equity]),
                   index=pd.to_datetime(np.concatenate([[res.times[0]], res.times + int(DAY_MS * 365 / res.bars_per_year)]), unit='ms'))
    q = eq.resample('QE').last()
    prev = q.shift().fillna(capital)
    return ((q / prev - 1) * 100).rename(res.name)


# --- CLI ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='Trend portfeli: Donçian ansamblı + volatillik ölçüsü + top-N rotasiya')
    p.add_argument('--days', type=int, default=730, help='qiymətləndirmə müddəti, gün (warm-up əlavə yüklənir)')
    p.add_argument('--end', type=int, default=None, help='dövrün sonu, epoch ms (default: son bağlanmış gün)')
    p.add_argument('--top', type=int, default=20)
    p.add_argument('--candidates', type=int, default=30, help='yüklənəcək namizəd coin sayı (×1.5)')
    p.add_argument('--symbols', nargs='*')
    p.add_argument('--target-vol', type=float, default=0.25)
    p.add_argument('--oos', type=float, default=0.33)
    p.add_argument('--only', nargs='*', help='yalnız bu variantlar')
    p.add_argument('--cache', default='data/klines')
    p.add_argument('--out', default='reports/trend')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    base = TrendParams(top=a.top, target_vol=a.target_vol)
    end = a.end or int(time.time() * 1000) // DAY_MS * DAY_MS
    warm_days = max(max(base.lookbacks_days), base.vol_days, base.turnover_days) + 2
    start = end - (a.days + warm_days) * DAY_MS
    source = BinanceData(a.cache)
    hourly, skipped = source.load_all(start, end, 0.97, a.symbols or liquid_symbols(source, a.candidates))
    if len(hourly) < 3:
        raise SystemExit('Kifayət qədər coin tapılmadı.')
    log.info('%d coin (tam tarixçəsi olmayan %d buraxıldı)', len(hourly), len(skipped))
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    rows, quarters, curves, results = [], [], {}, {}
    frames = {tf: resample(hourly, BAR_MS[tf]) for tf in {v[2] for v in VARIANTS}}
    for name, long_only, tf in VARIANTS:
        if a.only and name not in a.only:
            continue
        t0 = time.time()
        params = TrendParams(**{**base.__dict__, 'long_only': long_only})
        data = frames[tf]
        bars = len(next(iter(data.values())))
        first = bars - a.days * (DAY_MS // BAR_MS[tf])            # evaluate exactly the last --days
        res = run(data, params, BAR_MS[tf], name, start=max(first, warmup_bars(params, BAR_MS[tf])))
        results[name] = res
        row = evaluate(res, params.capital, a.oos)
        row.update(timeframe=tf, long_only=long_only, seconds=round(time.time() - t0))
        rows.append(row)
        quarters.append(quarterly(res, params.capital))
        curves[name] = pd.Series(res.equity, index=pd.to_datetime(res.times, unit='ms'))
        pd.DataFrame(res.weights, columns=res.symbols, index=pd.to_datetime(res.times, unit='ms')).round(4).to_csv(out / f'weights_{name}.csv')
        log.info('%-14s gəlir %+.1f%%  Sharpe %.2f  OOS Sharpe %.2f  maks. geriləmə %.1f%%  %d əməliyyat',
                 name, row['return_pct'], row['sharpe'], row['oos_sharpe'], row['max_dd_pct'], row['fills'])
    if not rows:
        raise SystemExit('Variant seçilmədi.')
    ref_name = 'long_short_1d' if 'long_short_1d' in results else next(iter(results))
    ref_tf = next(v[2] for v in VARIANTS if v[0] == ref_name)
    bench = benchmarks(frames[ref_tf], results[ref_name], base.capital, a.oos, a.top)
    summary = pd.DataFrame(rows + bench)
    summary.to_csv(out / 'summary.csv', index=False)
    pd.concat(quarters, axis=1).round(2).to_csv(out / 'quarterly.csv')
    pd.DataFrame(curves).ffill().to_csv(out / 'equity.csv')

    print(f'\n{len(hourly)} coin, son {a.days} gün, kapital {base.capital:g} $, hədəf volatillik {base.target_vol:.0%}. '
          f'Keçid şərti: Sharpe>1, OOS Sharpe>0.5 və OOS gəlir>0, maks. geriləmə<20%, ≥200 əməliyyat\n')
    print(f"{'Variant':26} {'Gəlir%':>8} {'İllik%':>7} {'Sharpe':>6} {'Geril.%':>7} {'IS%':>7} {'OOS%':>7} "
          f"{'OOS Sh':>6} {'Əməl.':>5} {'Kom.$':>6} {'Fand.$':>6}  Keçdi")
    for r in rows + bench:
        print(f"{r['name'][:26]:26} {r['return_pct']:8.1f} {r['cagr_pct']:7.1f} {r['sharpe']:6.2f} {r['max_dd_pct']:7.1f} "
              f"{r['is_return_pct']:7.1f} {r['oos_return_pct']:7.1f} {r['oos_sharpe']:6.2f} {r['fills']:5d} "
              f"{r['fees']:6.0f} {r['funding']:6.0f}  {'BƏLİ' if r['passed'] else '-'}")
    passed = [r['name'] for r in rows if r['passed']]
    print('\nŞərtləri keçənlər: ' + ', '.join(passed) if passed else '\nHeç bir trend variantı şərtləri keçmədi.')
    zip_path = shutil.make_archive(str(Path.home() / f'trend-{time.strftime("%Y%m%d-%H%M")}'), 'zip', out)
    print(f'\nZip: {zip_path}')


if __name__ == '__main__':
    main()
