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


@dataclass
class SimResult:
    key: str
    name: str
    equity: np.ndarray                      # account equity at every bar close
    trades: list[dict] = field(default_factory=list)


def _crossed(pos: Position, price: float, level: float, adverse: bool) -> bool:
    """Did ``price`` reach ``level``? adverse=True for stop / liquidation, False for take-profit."""
    if not level:
        return False
    toward_loss = price <= level if pos.side > 0 else price >= level
    return toward_loss if adverse else (price >= level if pos.side > 0 else price <= level)


def simulate(strategies: list[Strategy], data: dict[str, pd.DataFrame], settings: Settings) -> dict[str, SimResult]:
    """Replay every strategy on its own virtual account over aligned 1h frames."""
    symbols = list(data)
    index = next(iter(data.values())).index
    n = len(index)
    keys = [s.key for s in strategies]
    store = Store(':memory:')
    ledger = Ledger(settings, store, keys, int(index[0]))
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
                if t + 1 < n:
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
                if settings.cooldown_bars and last is not None and ts - last < settings.cooldown_bars * HOUR_MS:
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
                elif hold_ms and ts + HOUR_MS - pos.opened_at >= hold_ms:
                    close(k, pos, c[s][t], ts + HOUR_MS, 'timeout')
        # 4) mark to market at the close
        eq = ledger.update_prices({s: c[s][t] for s in symbols})
        for k in keys:
            equity[k][t] = eq[k]
    end = int(index[-1]) + HOUR_MS
    for k in keys:                                                   # liquidate what is still open
        for s, pos in list(ledger.accounts[k].positions.items()):
            close(k, pos, c[s][-1], end, 'end')
        equity[k][-1] = ledger.equity(k)
    store.close()
    return {k: SimResult(k, by_key[k].name, equity[k], trades[k]) for k in keys}


# --- metrics ---------------------------------------------------------------------------------
def metrics(eq: np.ndarray, trades: list[dict], capital: float) -> dict:
    eq = np.asarray(eq, dtype=float)
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    peak = np.maximum.accumulate(eq)
    pnl = np.array([t['pnl'] for t in trades]) if trades else np.array([])
    win, loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    reasons = pd.Series([t['reason'] for t in trades]).value_counts().to_dict() if trades else {}
    std = rets.std()
    return dict(
        final=float(eq[-1]), return_pct=float((eq[-1] / capital - 1) * 100),
        max_dd_pct=float((1 - eq / peak).max() * 100),
        sharpe=float(rets.mean() / std * math.sqrt(24 * 365)) if std > 0 else 0.0,
        trades=int(len(pnl)), win_pct=float((pnl > 0).mean() * 100) if len(pnl) else 0.0,
        profit_factor=float(win / loss) if loss > 0 else (math.inf if win > 0 else 0.0),
        fees=float(sum(t['fees'] for t in trades)), avg_lev=float(np.mean([t['leverage'] for t in trades])) if trades else 0.0,
        tp=int(reasons.get('take_profit', 0)), stop=int(reasons.get('stop_loss', 0)), liq=int(reasons.get('liquidation', 0)),
        signal=int(reasons.get('signal', 0) + reasons.get('reverse', 0)),
    )


def segment_return(eq: np.ndarray, a: int, b: int) -> float:
    """Return of an equity curve between bar a (start) and bar b (end, exclusive)."""
    start = eq[a - 1] if a > 0 else eq[0]
    return float(eq[b - 1] / start - 1) if start > 0 else -1.0


def segment_sharpe(eq: np.ndarray, a: int, b: int) -> float:
    seg = eq[max(a - 1, 0):b]
    r = np.diff(seg) / seg[:-1] if len(seg) > 1 else np.array([0.0])
    return float(r.mean() / r.std() * math.sqrt(24 * 365)) if r.std() > 0 else 0.0


def out_of_sample(results: dict[str, SimResult], split: int) -> pd.DataFrame:
    """Return of each strategy before and after ``split`` and the rank correlation between the two."""
    rows = []
    for k, r in results.items():
        rows.append(dict(key=k, name=r.name, in_sample_pct=segment_return(r.equity, 0, split) * 100,
                         out_sample_pct=segment_return(r.equity, split, len(r.equity)) * 100))
    df = pd.DataFrame(rows)
    df['is_rank'] = df.in_sample_pct.rank(ascending=False).astype(int)
    df['oos_rank'] = df.out_sample_pct.rank(ascending=False).astype(int)
    return df.sort_values('is_rank').reset_index(drop=True)


def walk_forward(results: dict[str, SimResult], train: int, test: int, top: int, by: str = 'sharpe') -> pd.DataFrame:
    """Every ``test`` bars: pick the ``top`` strategies by their last ``train`` bars, hold them ``test`` bars."""
    keys = list(results)
    n = len(next(iter(results.values())).equity)
    rows = []
    for start in range(train, n - 1, test):
        end = min(start + test, n)
        score = {k: (segment_sharpe(results[k].equity, start - train, start) if by == 'sharpe'
                     else segment_return(results[k].equity, start - train, start)) for k in keys}
        picked = sorted(keys, key=lambda k: score[k], reverse=True)[:top]
        nxt = {k: segment_return(results[k].equity, start, end) for k in keys}
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
    p.add_argument('--cache', default='data/klines', help='şam keşi qovluğu')
    p.add_argument('--out', default='reports/futures_bt', help='CSV nəticələr qovluğu')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    settings = replace(Settings.load(), timeframe='1h')
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
    results = simulate(strategies, data, settings)
    log.info('Simulyasiya %.0f s çəkdi', time.time() - t0)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cap = settings.capital
    summary = pd.DataFrame([{'key': k, 'name': r.name, **metrics(r.equity, r.trades, cap)} for k, r in results.items()])
    summary = summary.sort_values('return_pct', ascending=False).reset_index(drop=True)
    btc = data.get('BTCUSDT')
    n = len(next(iter(results.values())).equity)
    split = int(n * (1 - a.oos))
    oos = out_of_sample(results, split)
    wf = walk_forward(results, a.train_days * 24, a.test_days * 24, a.pick)
    summary.to_csv(out / 'summary.csv', index=False)
    oos.to_csv(out / 'out_of_sample.csv', index=False)
    wf.to_csv(out / 'walk_forward.csv', index=False)
    pd.DataFrame([{'strategy': k, **t, 'side': SIDE[t['side']]} for k, r in results.items() for t in r.trades]).to_csv(
        out / 'trades.csv', index=False)

    print(f'\n{len(data)} coin, {len(results)} strategiya, {a.days} gün, 1h — canlı qaydalar '
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
