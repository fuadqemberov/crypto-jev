"""Futures backtest that replays the LIVE rules on history, plus out-of-sample and walk-forward tests.

    python -m backtest.futures_sim --days 365 --top 50

Unlike ``python -m backtest`` (spot, long only, 1x) this uses the live engine's own accounting
(``app.ledger.Ledger``) and rules (``app.futures``) on 1h candles:

* LONG from the rule, SHORT from the same rule on the mirrored series; fresh signal -> order at the next
  bar's open (the live engine fills at the mark right after the candle closes);
* leverage = floor(RISK_PER_MOVE / (ATR% x multiple)), MAX_COST_R fee filter, margin = ORDER_MARGIN_PCT;
* stop-loss and 1.5R take-profit fixed at the open and checked against every bar: a gap through a level
  fills at the open, a wick fills at the level, and if one bar touches both the stop is assumed (worst
  case); liquidation only if the price jumps past the stop to the liquidation price;
* signal exits / reversals at the next open, MAX_HOLD_HOURS, taker fee + slippage on both sides.

Not modelled: funding payments (no history in the public spot data) and the difference between
spot and perpetual prices. Settings come from .env exactly like the live bot (timeframe forced to 1h).

Two robustness tests answer "would picking the winners have worked?":

* out-of-sample: rank strategies on the first part of the period, check the ranking on the unseen rest;
* walk-forward: every TEST days pick the TOP strategies by their last TRAIN days and follow them for
  the next TEST days — the procedure used by hand on the live dashboard.
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import Settings
from app.futures import cost_r, direction, exits_on_signal, leverage, stop_distance
from app.ledger import Ledger, Position
from app.store import Store

from . import indicators as ind
from .data import HOUR_MS, BinanceData
from .strategies import STRATEGIES, Strategy

log = logging.getLogger('backtest.futures')
SIDE = {1: 'LONG', -1: 'SHORT'}
BAR_MS = {'1h': HOUR_MS, '2h': 2 * HOUR_MS, '4h': 4 * HOUR_MS, '6h': 6 * HOUR_MS, '12h': 12 * HOUR_MS, '1d': 24 * HOUR_MS}


@dataclass
class SimResult:
    key: str
    name: str
    equity: np.ndarray                      # account equity at every bar close
    trades: list[dict] = field(default_factory=list)
    times: np.ndarray | None = None         # bar open times (ms)
    capital: float = 2000.0
    fixed: bool = False                     # fixed margin per order: returns are measured against the capital
    bars_per_year: float = 24 * 365


class FixedMarginLedger(Ledger):
    """Every order uses the same margin, whatever the balance — the account can never "run dry",
    so a strategy is measured on its whole period (needed for a fair out-of-sample test)."""

    def __init__(self, *args, margin: float, **kw) -> None:
        super().__init__(*args, **kw)
        self.fixed_margin = margin

    def order_margin(self, key: str, leverage: int) -> float:
        return self.fixed_margin


def resample(data: dict[str, pd.DataFrame], bar_ms: int) -> dict[str, pd.DataFrame]:
    """1h frames -> bar_ms frames (e.g. 4h) aligned to UTC multiples of bar_ms; drops a trailing partial bar."""
    if bar_ms == HOUR_MS:
        return data
    out = {}
    for sym, df in data.items():
        g = df.index.values // bar_ms * bar_ms
        agg = df.groupby(g).agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
                                close=('close', 'last'), volume=('volume', 'sum'), n=('close', 'size'))
        full = agg[agg.n == bar_ms // HOUR_MS].drop(columns='n')
        full.index = pd.Index(full.index.astype('int64'), name='time')
        out[sym] = full
    common = sorted(set.intersection(*(set(f.index) for f in out.values())))
    return {s: f.loc[common] for s, f in out.items()}


def _crossed(pos: Position, price: float, level: float, adverse: bool) -> bool:
    """Did ``price`` reach ``level``? adverse=True for stop / liquidation, False for take-profit."""
    if not level:
        return False
    toward_loss = price <= level if pos.side > 0 else price >= level
    return toward_loss if adverse else (price >= level if pos.side > 0 else price <= level)


def simulate(strategies: list[Strategy], data: dict[str, pd.DataFrame], settings: Settings, *,
             side: str = 'both', fixed_margin: float = 0.0, bar_ms: int = HOUR_MS) -> dict[str, SimResult]:
    """Replay every strategy on its own virtual account over aligned frames of ``bar_ms``.

    side: 'both' | 'long' | 'short' — which fresh signals may open; fixed_margin > 0: every order uses
    that margin (see FixedMarginLedger). Exits follow settings.signal_exits ('all' / 'auto' / 'none')."""
    symbols = list(data)
    index = next(iter(data.values())).index
    n = len(index)
    keys = [s.key for s in strategies]
    store = Store(':memory:')
    ledger = (FixedMarginLedger(settings, store, keys, int(index[0]), margin=fixed_margin) if fixed_margin > 0
              else Ledger(settings, store, keys, int(index[0])))
    allowed = {'both': (1, -1), 'long': (1,), 'short': (-1,)}[side]
    o = {s: data[s].open.to_numpy(float) for s in symbols}
    h = {s: data[s].high.to_numpy(float) for s in symbols}
    lo = {s: data[s].low.to_numpy(float) for s in symbols}
    c = {s: data[s].close.to_numpy(float) for s in symbols}
    atr = {s: (ind.atr(data[s], 14) / data[s].close).to_numpy(float) for s in symbols}
    dirs = {k: {} for k in keys}
    for st in strategies:
        for s in symbols:
            dirs[st.key][s] = direction(st, data[s])
    # fresh entries decided at the close of bar t-1 are executed at the open of bar t
    entries: list[list[tuple[str, str, int]]] = [[] for _ in range(n)]
    for k in keys:
        for s, d in dirs[k].items():
            prev = np.concatenate([[0], d[:-1]])
            for t in np.flatnonzero((d != 0) & (d != prev)):
                if t + 1 < n and int(d[t]) in allowed:
                    entries[t + 1].append((k, s, int(d[t])))
    by_key = {s.key: s for s in strategies}
    hold_ms = int(settings.max_hold_hours * 3_600_000)
    cost_rate = settings.fee_rate + settings.slippage_rate
    trades: dict[str, list[dict]] = {k: [] for k in keys}
    equity = {k: np.empty(n) for k in keys}
    last_close: dict[tuple[str, str], int] = {}

    def close(k: str, pos: Position, price: float, ts: int, reason: str) -> None:
        tr = ledger.close(k, pos.symbol, price, ts, reason)
        if tr:
            trades[k].append(tr)
            last_close[(k, pos.symbol)] = ts

    for t in range(n):
        ts = int(index[t])
        if t:
            # 1) signal exits / reversals decided at the previous close -> fill at this open
            for k in keys:
                if not exits_on_signal(k, settings.signal_exits):
                    continue
                for s, pos in list(ledger.accounts[k].positions.items()):
                    cur = int(dirs[k][s][t - 1])
                    if cur != pos.side:
                        close(k, pos, o[s][t], ts, 'reverse' if cur == -pos.side else 'signal')
            # 2) fresh entries at this open (same filters as the live engine)
            for k, s, side in entries[t]:
                acc = ledger.accounts[k]
                if s in acc.positions:
                    continue
                last = last_close.get((k, s))
                if settings.cooldown_bars and last is not None and ts - last < settings.cooldown_bars * bar_ms:
                    continue
                a = float(atr[s][t - 1])
                if settings.max_cost_r > 0 and cost_r(k, a, cost_rate) > settings.max_cost_r:
                    continue
                px = o[s][t]
                lev = leverage(k, a, settings.max_leverage, settings.risk_per_move)
                ledger.open(k, s, side, lev, px, ts, stop_distance(k, a, px))
        # 3) the bar itself: open gap, then the high / low range (stop first if both are touched)
        for k in keys:
            for s, pos in list(ledger.accounts[k].positions.items()):
                liq = ledger.liquidation_price(pos)
                first = o[s][t] if pos.opened_at < ts else None          # a position opened at this open has no gap
                worst = lo[s][t] if pos.side > 0 else h[s][t]
                best = h[s][t] if pos.side > 0 else lo[s][t]
                if first is not None and _crossed(pos, first, pos.stop_price, True):
                    close(k, pos, first, ts, 'liquidation' if _crossed(pos, first, liq, True) else 'stop_loss')
                elif first is not None and not pos.stop_price and _crossed(pos, first, liq, True):
                    close(k, pos, liq, ts, 'liquidation')
                elif first is not None and _crossed(pos, first, pos.tp_price, False):
                    close(k, pos, first, ts, 'take_profit')
                elif _crossed(pos, worst, pos.stop_price, True):
                    close(k, pos, pos.stop_price, ts, 'stop_loss')
                elif not pos.stop_price and _crossed(pos, worst, liq, True):
                    close(k, pos, liq, ts, 'liquidation')
                elif _crossed(pos, best, pos.tp_price, False):
                    close(k, pos, pos.tp_price, ts, 'take_profit')
                elif settings.take_profit > 0 and ledger.net_pnl(pos, c[s][t]) >= settings.take_profit:
                    close(k, pos, c[s][t], ts, 'take_profit')
                elif hold_ms and ts + bar_ms - pos.opened_at >= hold_ms:
                    close(k, pos, c[s][t], ts + bar_ms, 'timeout')
        # 4) mark to market at the close
        eq = ledger.update_prices({s: c[s][t] for s in symbols})
        for k in keys:
            equity[k][t] = eq[k]
    end = int(index[-1]) + bar_ms
    for k in keys:                                                   # liquidate what is still open
        for s, pos in list(ledger.accounts[k].positions.items()):
            close(k, pos, c[s][-1], end, 'end')
        equity[k][-1] = ledger.equity(k)
    store.close()
    times = np.asarray(index, dtype='int64')
    bpy = 365 * 24 * HOUR_MS / bar_ms
    return {k: SimResult(k, by_key[k].name, equity[k], trades[k], times, settings.capital, fixed_margin > 0, bpy)
            for k in keys}


# --- metrics ---------------------------------------------------------------------------------
def _rets(eq: np.ndarray, capital: float, fixed: bool) -> np.ndarray:
    if len(eq) < 2:
        return np.array([0.0])
    return np.diff(eq) / (capital if fixed else eq[:-1])


def _pf(pnl: np.ndarray) -> float:
    win, loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return float(win / loss) if loss > 0 else (math.inf if win > 0 else 0.0)


def metrics(eq: np.ndarray, trades: list[dict], capital: float, fixed: bool = False, bars_per_year: float = 24 * 365) -> dict:
    eq = np.asarray(eq, dtype=float)
    rets = _rets(eq, capital, fixed)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / capital if fixed else 1 - eq / peak
    pnl = np.array([t['pnl'] for t in trades]) if trades else np.array([])
    reasons = pd.Series([t['reason'] for t in trades]).value_counts().to_dict() if trades else {}
    std = rets.std()
    return dict(
        final=float(eq[-1]), return_pct=float((eq[-1] / capital - 1) * 100),
        max_dd_pct=float(dd.max() * 100),
        sharpe=float(rets.mean() / std * math.sqrt(bars_per_year)) if std > 0 else 0.0,
        trades=int(len(pnl)), win_pct=float((pnl > 0).mean() * 100) if len(pnl) else 0.0,
        profit_factor=_pf(pnl),
        fees=float(sum(t['fees'] for t in trades)), avg_lev=float(np.mean([t['leverage'] for t in trades])) if trades else 0.0,
        tp=int(reasons.get('take_profit', 0)), stop=int(reasons.get('stop_loss', 0)), liq=int(reasons.get('liquidation', 0)),
        signal=int(reasons.get('signal', 0) + reasons.get('reverse', 0)),
        long_pnl=float(sum(t['pnl'] for t in trades if t['side'] == 1)),
        short_pnl=float(sum(t['pnl'] for t in trades if t['side'] == -1)),
    )


def segment_return(eq: np.ndarray, a: int, b: int, capital: float | None = None) -> float:
    """Return between bar a (start) and bar b (end, exclusive); with ``capital`` (fixed margin) as P&L / capital."""
    start = eq[a - 1] if a > 0 else (capital if capital else eq[0])
    if capital:
        return float((eq[b - 1] - start) / capital)
    return float(eq[b - 1] / start - 1) if start > 0 else -1.0


def segment_sharpe(eq: np.ndarray, a: int, b: int, capital: float | None = None, bars_per_year: float = 24 * 365) -> float:
    seg = eq[max(a - 1, 0):b]
    r = _rets(seg, capital or 0.0, bool(capital))
    return float(r.mean() / r.std() * math.sqrt(bars_per_year)) if r.std() > 0 else 0.0


def _cap(r: SimResult) -> float | None:
    return r.capital if r.fixed else None


def out_of_sample(results: dict[str, SimResult], split: int) -> pd.DataFrame:
    """Return / profit factor of each strategy before and after bar ``split``."""
    rows = []
    for k, r in results.items():
        cut = int(r.times[split]) if r.times is not None and split < len(r.times) else None
        before = np.array([t['pnl'] for t in r.trades if cut is None or t['opened_at'] < cut])
        after = np.array([t['pnl'] for t in r.trades if cut is not None and t['opened_at'] >= cut])
        rows.append(dict(key=k, name=r.name, in_sample_pct=segment_return(r.equity, 0, split, _cap(r)) * 100,
                         out_sample_pct=segment_return(r.equity, split, len(r.equity), _cap(r)) * 100,
                         is_pf=_pf(before), oos_pf=_pf(after), is_trades=len(before), oos_trades=len(after)))
    df = pd.DataFrame(rows)
    df['is_rank'] = df.in_sample_pct.rank(ascending=False, method='first').astype(int)
    df['oos_rank'] = df.out_sample_pct.rank(ascending=False, method='first').astype(int)
    return df.sort_values('is_rank').reset_index(drop=True)


def walk_forward(results: dict[str, SimResult], train: int, test: int, top: int, by: str = 'sharpe') -> pd.DataFrame:
    """Every ``test`` bars: pick the ``top`` strategies by their last ``train`` bars, hold them ``test`` bars."""
    keys = list(results)
    n = len(next(iter(results.values())).equity)
    rows = []
    for start in range(train, n - 1, test):
        end = min(start + test, n)
        r0 = results[keys[0]]
        score = {k: (segment_sharpe(results[k].equity, start - train, start, _cap(results[k]), r0.bars_per_year)
                     if by == 'sharpe' else segment_return(results[k].equity, start - train, start, _cap(results[k])))
                 for k in keys}
        picked = sorted(keys, key=lambda k: score[k], reverse=True)[:top]
        nxt = {k: segment_return(results[k].equity, start, end, _cap(results[k])) for k in keys}
        rows.append(dict(start=start, end=end, picked=','.join(picked),
                         picked_pct=float(np.mean([nxt[k] for k in picked]) * 100),
                         all_pct=float(np.mean(list(nxt.values())) * 100),
                         worst_last_pct=float(np.mean([nxt[k] for k in sorted(keys, key=lambda k: score[k])[:top]]) * 100)))
    return pd.DataFrame(rows)


def liquid_symbols(source: BinanceData, n: int) -> list[str]:
    """The ~1.5·n spot USDT coins with the largest 24h quote volume — downloaded instead of all ~400."""
    allowed = set(source.symbols())
    tickers = source._get('/ticker/24hr')
    vol = {t['symbol']: float(t.get('quoteVolume') or 0) for t in tickers if t.get('symbol') in allowed}
    return sorted(vol, key=vol.get, reverse=True)[:max(n, int(n * 1.5))]


def top_by_volume(data: dict[str, pd.DataFrame], n: int) -> dict[str, pd.DataFrame]:
    turnover = {s: float((df.close * df.volume).mean()) for s, df in data.items()}
    keep = sorted(turnover, key=turnover.get, reverse=True)[:n]
    return {s: data[s] for s in sorted(keep)}


# --- CLI ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='Canlı qaydalarla futures backtest + out-of-sample + walk-forward (1h)')
    p.add_argument('--days', type=int, default=365, help='test müddəti, gün (default 365)')
    p.add_argument('--end', type=int, default=None, help='dövrün sonu, epoch ms (default: son bağlanmış saat)')
    p.add_argument('--top', type=int, default=50, help='orta dövriyyəyə görə ən böyük N coin (canlıdakı TOP_N kimi)')
    p.add_argument('--symbols', nargs='*', help='konkret coin siyahısı (məs. BTCUSDT ETHUSDT)')
    p.add_argument('--oos', type=float, default=0.33, help='sonda out-of-sample payı (0.33 = son 1/3)')
    p.add_argument('--train-days', type=int, default=30, help='walk-forward: seçim üçün geriyə baxış, gün')
    p.add_argument('--test-days', type=int, default=7, help='walk-forward: seçilmişləri izləmə müddəti, gün')
    p.add_argument('--pick', type=int, default=5, help='walk-forward: hər dəfə seçilən strategiya sayı')
    p.add_argument('--strategies', nargs='*', help='yalnız bu açarlar (məs. ema_pullback williams)')
    p.add_argument('--side', choices=('both', 'long', 'short'), default='both', help='hansı siqnallar açılsın')
    p.add_argument('--signal-exits', choices=('all', 'auto', 'none'), default=None,
                   help='all = əks siqnalda çıxış (canlı default), none = yalnız stop / TP / vaxt')
    p.add_argument('--timeframe', choices=('1h', '2h', '4h', '6h', '12h', '1d'), default='1h', help='şam (1h-dan qurulur)')
    p.add_argument('--fixed-margin', type=float, default=0.0,
                   help='hər order üçün sabit marja, USDT (məs. 100); 0 = canlıdakı kimi balansın faizi')
    p.add_argument('--cache', default='data/klines', help='şam keşi qovluğu')
    p.add_argument('--out', default='reports/futures_bt', help='CSV nəticələr qovluğu')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    settings = replace(Settings.load(), timeframe='1h')
    if a.signal_exits:
        settings = replace(settings, signal_exits=a.signal_exits)
    bar_ms = BAR_MS[a.timeframe]
    end = a.end or int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - a.days * 24 * HOUR_MS
    source = BinanceData(a.cache)
    data, _ = source.load_all(start, end, 0.97, a.symbols or liquid_symbols(source, a.top))
    if not a.symbols:
        data = top_by_volume(data, a.top)
    strategies = [s for s in STRATEGIES if not a.strategies or s.key in a.strategies]
    if not data or not strategies:
        raise SystemExit('Coin və ya strategiya tapılmadı.')
    log.info('%d strategiya × %d coin × %d saat simulyasiya olunur…', len(strategies), len(data), a.days * 24)
    t0 = time.time()
    data = resample(data, bar_ms)
    results = simulate(strategies, data, settings, side=a.side, fixed_margin=a.fixed_margin, bar_ms=bar_ms)
    log.info('Simulyasiya %.0f s çəkdi', time.time() - t0)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cap = settings.capital
    summary = pd.DataFrame([{'key': k, 'name': r.name, **metrics(r.equity, r.trades, cap, r.fixed, r.bars_per_year)}
                            for k, r in results.items()])
    summary = summary.sort_values('return_pct', ascending=False).reset_index(drop=True)
    btc = data.get('BTCUSDT')
    n = len(next(iter(results.values())).equity)
    split = int(n * (1 - a.oos))
    oos = out_of_sample(results, split)
    per_day = 24 * HOUR_MS // bar_ms
    wf = walk_forward(results, a.train_days * per_day, a.test_days * per_day, a.pick)
    summary.to_csv(out / 'summary.csv', index=False)
    oos.to_csv(out / 'out_of_sample.csv', index=False)
    wf.to_csv(out / 'walk_forward.csv', index=False)
    pd.DataFrame([{'strategy': k, **t, 'side': SIDE[t['side']]} for k, r in results.items() for t in r.trades]).to_csv(
        out / 'trades.csv', index=False)

    print(f'\n{len(data)} coin, {len(results)} strategiya, {a.days} gün, {a.timeframe}, {a.side}, '
          f'siqnal çıxışı {settings.signal_exits}, marja {a.fixed_margin or str(settings.margin_pct) + "%"} — canlı qaydalar '
          f'(risk {settings.risk_per_move}, TP {settings.tp_r_multiple}R, stop ≤{settings.stop_loss_pct}% marja, '
          f'komissiya filtri {settings.max_cost_r}, maks {settings.max_leverage}×)\n')
    print(f"{'#':>2} {'Strategiya':38} {'Gəlir%':>8} {'MaxDD%':>7} {'Sharpe':>6} {'Əməl.':>6} {'Uduş%':>6} {'PF':>5} {'Komis.$':>8} {'Likv':>4}")
    for i, r in summary.iterrows():
        print(f"{i + 1:>2} {r['name'][:38]:38} {r.return_pct:8.2f} {r.max_dd_pct:7.2f} {r.sharpe:6.2f} {r.trades:6d} "
              f"{r.win_pct:6.1f} {min(r.profit_factor, 99):5.2f} {r.fees:8.0f} {r.liq:4d}")
    if btc is not None:
        print(f"{'-':>2} {'BTC al-saxla (1×)':38} {(btc.close.iloc[-1] / btc.open.iloc[0] - 1) * 100:8.2f}")
    rho = oos[['in_sample_pct', 'out_sample_pct']].corr(method='spearman').iloc[0, 1]
    top5 = oos.head(5)
    print(f"\nOut-of-sample (ilk {1 - a.oos:.0%} → son {a.oos:.0%}): sıralama korrelyasiyası ρ = {rho:+.2f} "
          f"(+1 = qaliblər qalib qalır, 0 = təsadüf, −1 = tərsinə)")
    print(f"  İlk hissənin top 5-i: əvvəl ort. {top5.in_sample_pct.mean():+.2f}%, sonra ort. {top5.out_sample_pct.mean():+.2f}% "
          f"(bütün strategiyaların sonrakı ortası {oos.out_sample_pct.mean():+.2f}%)")
    if len(wf):
        comp = lambda col: (np.prod(1 + wf[col] / 100) - 1) * 100  # noqa: E731
        print(f"\nWalk-forward (hər {a.test_days} gündə son {a.train_days} günün ən yaxşı {a.pick}-i, {len(wf)} dövr):")
        print(f"  seçilmiş qaliblər: {comp('picked_pct'):+.2f}% | bütün strategiyalar: {comp('all_pct'):+.2f}% | "
              f"son {a.pick} (uduzanlar): {comp('worst_last_pct'):+.2f}%")
        print(f"  qaliblər ortadan yaxşı olan dövrlər: {(wf.picked_pct > wf.all_pct).mean():.0%}")
    print(f'\nCSV: {out.resolve()}/summary.csv, out_of_sample.csv, walk_forward.csv, trades.csv')


if __name__ == '__main__':
    main()
