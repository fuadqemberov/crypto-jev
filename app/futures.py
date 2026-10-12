"""Futures layer on top of the 20 long-only rules in ``backtest/strategies.py``.

Direction
    LONG  – the strategy's own long rule on the real price series.
    SHORT – the same rule applied to the *mirrored* series (every price p -> 1/p, high <-> low),
            i.e. the exact opposite market. A breakout down, an RSI drop through 70, a SMA20
            falling under SMA50 ... become short signals with no extra parameters.
    If both fire on one bar (rare, the mirror is not perfectly linear) the strategy stays flat.

Leverage (chosen by the strategy itself, per trade)
    Every strategy declares how much adverse movement it expects to sit through, in ATR(14)
    multiples, and its own leverage ceiling. On each entry::

        leverage = floor(0.5 / (ATR% × atr_mult)), clamped to [1, min(strategy_max, MAX_LEVERAGE)]

    so the strategy's typical adverse move costs at most ~50 % of the position margin, while
    liquidation sits roughly twice as far away. Calm coins get more leverage, wild ones less;
    tight mean-reversion rules accept more leverage than wide trend/breakout rules.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest import indicators as ind
from backtest.strategies import Strategy

log = logging.getLogger('app.futures')
RISK_PER_TYPICAL_MOVE = 0.5

# key -> (expected adverse move in ATR multiples, strategy's own max leverage)
LEVERAGE_PROFILE: dict[str, tuple[float, int]] = {
    'sma_cross': (3.0, 10), 'ema_cross': (3.0, 10), 'rsi': (2.0, 15), 'macd': (2.5, 12),
    'bb_revert': (2.0, 15), 'bb_break': (3.0, 8), 'stoch': (2.0, 12), 'adx': (3.0, 10),
    'ichimoku': (3.0, 10), 'vwap': (2.5, 12), 'supertrend': (3.0, 10), 'donchian': (3.5, 8),
    'williams': (2.0, 15), 'cci': (2.0, 15), 'psar': (2.5, 10), 'keltner': (3.0, 8),
    'atr_trail': (3.0, 10), 'tema': (2.5, 12), 'mfi': (2.0, 12), 'rsi_macd': (2.5, 12),
}
DEFAULT_PROFILE = (3.0, 10)


@dataclass(frozen=True)
class Signal:
    prev: int          # -1 short / 0 flat / 1 long on the bar before
    cur: int           # direction on the bar that just closed
    atr_pct: float     # ATR(14) / close on that bar


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """The opposite market: price p -> 1/p (high and low swap), volume unchanged."""
    return pd.DataFrame({'open': 1 / df.open, 'high': 1 / df.low, 'low': 1 / df.high,
                         'close': 1 / df.close, 'volume': df.volume}, index=df.index)


def direction(strategy: Strategy, df: pd.DataFrame, mirrored: pd.DataFrame | None = None) -> np.ndarray:
    long_ = np.nan_to_num(np.asarray(strategy.fn(df), dtype=float)) > 0.5
    short = np.nan_to_num(np.asarray(strategy.fn(mirror(df) if mirrored is None else mirrored), dtype=float)) > 0.5
    return long_.astype(int) - short.astype(int)


def leverage(key: str, atr_pct: float, cap: int) -> int:
    mult, own_max = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    ceiling = max(1, min(own_max, cap))
    if not math.isfinite(atr_pct) or atr_pct <= 0:
        return 1
    return int(min(ceiling, max(1, math.floor(RISK_PER_TYPICAL_MOVE / (atr_pct * mult)))))


def leverage_rule(key: str, cap: int) -> str:
    mult, own_max = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    return f'Leverage: ⌊0.5 / (ATR% × {mult:g})⌋, 1× – {min(own_max, cap)}× (strategiyanın öz tavanı {own_max}×).'


def evaluate(strategies: list[Strategy], data: dict[str, pd.DataFrame]) -> dict[str, dict[str, Signal]]:
    out: dict[str, dict[str, Signal]] = {s.key: {} for s in strategies}
    for symbol, df in data.items():
        mirrored = mirror(df)
        atr = ind.atr(df, 14).iloc[-1] / df.close.iloc[-1]
        for s in strategies:
            try:
                d = direction(s, df, mirrored)
            except Exception:  # one broken frame must not stop the other strategies
                log.exception('%s %s strategiya xətası', s.key, symbol)
                continue
            if len(d) >= 2:
                out[s.key][symbol] = Signal(int(d[-2]), int(d[-1]), float(atr) if pd.notna(atr) else float('nan'))
    return out
