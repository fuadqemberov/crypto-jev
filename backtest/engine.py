"""Vectorized long-only spot backtester.

Each strategy owns an independent virtual account (default 2000 USDT) split equally into one
sub-account per coin. Decisions are taken on a bar close and filled at the next bar open with
fee + slippage on every side. Equity is marked to market at every 1h close.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategies import STRATEGIES, Strategy

HOURS_PER_YEAR = 24 * 365


@dataclass
class CoinResult:
    symbol: str
    equity: np.ndarray            # sub-account equity at each close
    trade_returns: np.ndarray     # net return per closed trade
    trade_pnl: np.ndarray         # USDT P&L per trade
    holding_hours: np.ndarray
    exposure: float


@dataclass
class StrategyResult:
    strategy: Strategy
    equity: pd.Series
    coins: dict[str, CoinResult] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)


def simulate_coin(df: pd.DataFrame, pos: np.ndarray, capital: float, cost: float, symbol: str = '') -> CoinResult:
    o, c = df.open.to_numpy(), df.close.to_numpy()
    pos = np.nan_to_num(np.asarray(pos, dtype=float))
    held = np.concatenate([[0.0], pos[:-1]])          # position during bar t was decided at close t-1
    prev_held = np.concatenate([[0.0], held[:-1]])
    prev_close = np.concatenate([[o[0]], c[:-1]])
    gap = 1 + prev_held * (o / prev_close - 1)          # close[t-1] -> open[t]
    body = 1 + held * (c / o - 1)                       # open[t] -> close[t]
    fees = (1 - cost) ** np.abs(held - prev_held)
    equity = capital * np.cumprod(gap * body * fees)
    if held[-1]:
        equity[-1] *= 1 - cost                          # liquidate the open position at the final close

    change = np.diff(np.concatenate([[0.0], held, [0.0]]))
    entries, exits = np.flatnonzero(change > 0), np.flatnonzero(change < 0)
    entry_px = o[entries]
    exit_px = np.where(exits < len(o), o[np.minimum(exits, len(o) - 1)], c[-1])
    rets = exit_px / entry_px * (1 - cost) ** 2 - 1
    cash = np.where(entries > 0, equity[np.maximum(entries - 1, 0)], capital)
    return CoinResult(symbol, equity, rets, cash * rets, (exits - entries).astype(float), float(held.mean()))


def metrics(equity: pd.Series, coins: dict[str, CoinResult], capital: float) -> dict:
    eq = equity.to_numpy()
    rets = np.diff(eq) / eq[:-1]
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    years = len(eq) / HOURS_PER_YEAR
    total = eq[-1] / capital - 1
    cagr = (eq[-1] / capital) ** (1 / years) - 1 if eq[-1] > 0 else -1.0
    std, downside = rets.std(), rets[rets < 0].std() if (rets < 0).any() else 0.0
    trades = np.concatenate([r.trade_returns for r in coins.values()]) if coins else np.array([])
    pnl = np.concatenate([r.trade_pnl for r in coins.values()]) if coins else np.array([])
    hold = np.concatenate([r.holding_hours for r in coins.values()]) if coins else np.array([])
    gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    coin_ret = {s: r.equity[-1] / (capital / len(coins)) - 1 for s, r in coins.items()} if coins else {}
    max_dd = float(-dd.min())
    return dict(
        final_balance=float(eq[-1]), total_return=float(total * 100), net_profit=float(eq[-1] - capital),
        cagr=float(cagr * 100), max_drawdown=max_dd * 100,
        sharpe=float(rets.mean() / std * np.sqrt(HOURS_PER_YEAR)) if std > 0 else 0.0,
        sortino=float(rets.mean() / downside * np.sqrt(HOURS_PER_YEAR)) if downside > 0 else 0.0,
        calmar=float(cagr / max_dd) if max_dd > 0 else 0.0,
        volatility=float(std * np.sqrt(HOURS_PER_YEAR) * 100),
        trades=int(len(trades)), win_rate=float((trades > 0).mean() * 100) if len(trades) else 0.0,
        profit_factor=float(gross_win / gross_loss) if gross_loss > 0 else (float('inf') if gross_win > 0 else 0.0),
        avg_trade=float(trades.mean() * 100) if len(trades) else 0.0,
        best_trade=float(trades.max() * 100) if len(trades) else 0.0,
        worst_trade=float(trades.min() * 100) if len(trades) else 0.0,
        avg_hold_hours=float(hold.mean()) if len(hold) else 0.0,
        exposure=float(np.mean([r.exposure for r in coins.values()]) * 100) if coins else 0.0,
        profitable_coins=int(sum(v > 0 for v in coin_ret.values())), coin_count=len(coin_ret),
    )


def _run_strategy(strategy: Strategy, data: dict[str, pd.DataFrame], capital: float, cost: float) -> StrategyResult:
    alloc = capital / len(data)
    coins = {sym: simulate_coin(df, strategy.fn(df), alloc, cost, sym) for sym, df in data.items()}
    index = next(iter(data.values())).index
    equity = pd.Series(np.sum([r.equity for r in coins.values()], axis=0), index=index)
    result = StrategyResult(strategy, equity, coins)
    result.metrics = metrics(equity, coins, capital)
    return result


def run_all(data: dict[str, pd.DataFrame], capital: float = 2000.0, cost: float = 0.0015,
            strategies: list[Strategy] | None = None) -> tuple[list[StrategyResult], StrategyResult]:
    if not data:
        raise ValueError('Backtest üçün heç bir coin məlumatı yoxdur.')
    results = [_run_strategy(s, data, capital, cost) for s in (strategies or STRATEGIES)]
    hodl = Strategy('buy_hold', 'Al və saxla (benchmark)', 'Benchmark',
                    'Bütün coinlərə bərabər bölünmüş kapital dövrün əvvəlində alınır və sonda satılır.',
                    lambda df: np.ones(len(df)))
    benchmark = _run_strategy(hodl, data, capital, cost)
    results.sort(key=lambda r: r.metrics['total_return'], reverse=True)
    return results, benchmark
