"""Futures layer on top of the 26 long-only rules in ``backtest/strategies.py``.

Direction
    LONG  – the strategy's own long rule on the real price series.
    SHORT – the same rule applied to the *mirrored* series (every price p -> 1/p, high <-> low),
            i.e. the exact opposite market. A breakout down, an RSI drop through 70, a SMA20
            falling under SMA50 ... become short signals with no extra parameters.
    If both fire on one bar (rare, the mirror is not perfectly linear) the strategy stays flat.

Leverage (chosen by the strategy itself, per trade)
    Every strategy declares how much adverse movement it expects to sit through, in ATR(14)
    multiples. On each entry::

        leverage = floor(RISK_PER_MOVE / (ATR% × atr_mult)), clamped to [1×, MAX_LEVERAGE] (default 1×–100×)

    so the strategy's typical adverse move (= its stop distance) costs ~RISK_PER_MOVE (default 25 %)
    of the position margin, while liquidation sits roughly four times as far away. Calm coins get more leverage, wild ones less;
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
RISK_PER_TYPICAL_MOVE = 0.25   # default of RISK_PER_MOVE

# key -> (expected adverse move in ATR multiples, leverage ceiling)
LEVERAGE_PROFILE: dict[str, tuple[float, int]] = {
    'sma_cross': (3.0, 100), 'ema_cross': (3.0, 100), 'rsi': (2.0, 100), 'macd': (2.5, 100),
    'bb_revert': (2.0, 100), 'bb_break': (3.0, 100), 'stoch': (2.0, 100), 'adx': (3.0, 100),
    'ichimoku': (3.0, 100), 'vwap': (2.5, 100), 'supertrend': (3.0, 100), 'donchian': (3.5, 100),
    'williams': (2.0, 100), 'cci': (2.0, 100), 'psar': (2.5, 100), 'keltner': (3.0, 100),
    'atr_trail': (3.0, 100), 'tema': (2.5, 100), 'mfi': (2.0, 100), 'rsi_macd': (2.5, 100),
    # YouTube tactics: the sweep and histogram rules use the same formula; the fixed 2:1 rule is
    # capped at the 20x its source trades with (its -0.5 % stop = -10 % of margin there).
    'liq_sweep': (2.0, 100), 'macd_hist': (2.5, 100), 'rr_2to1': (2.0, 20),
    # ceilings from the videos: 25x for the pull-back rules, 3-5x for the bot signals
    'ema_pullback': (2.5, 25), 'range_scalp': (2.0, 100), 'confluence': (3.0, 5),
}
DEFAULT_PROFILE = (3.0, 100)

# SIGNAL_EXITS=auto (experimental, off by default): rules whose exit signal IS their profit target / own
# stop keep closing on it — mean-reversion and rules with a built-in stop / target / trail; trend, momentum
# and breakout cross-overs then end only at stop / TP / max hold. Their signal exits lost money live
# (15–18 Oct 2026), but in demo runs replacing them with full stops was worse overall, hence 'all' default.
OWN_EXIT_RULES = frozenset({'rsi', 'bb_revert', 'stoch', 'williams', 'cci', 'mfi',
                            'atr_trail', 'liq_sweep', 'rr_2to1', 'ema_pullback', 'range_scalp'})


def exits_on_signal(key: str, mode: str) -> bool:
    return mode == 'all' or (mode == 'auto' and key in OWN_EXIT_RULES)


def cost_r(key: str, atr_pct: float, cost_rate: float) -> float:
    """Round-trip fee + slippage as a share of the stop distance (1R). inf when ATR is unknown."""
    mult, _ = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    if not math.isfinite(atr_pct) or atr_pct <= 0:
        return math.inf
    return 2 * cost_rate / (mult * atr_pct)


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


def leverage(key: str, atr_pct: float, cap: int, risk: float = RISK_PER_TYPICAL_MOVE) -> int:
    mult, own_max = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    ceiling = max(1, min(own_max, cap))
    if not math.isfinite(atr_pct) or atr_pct <= 0:
        return 1
    return int(min(ceiling, max(1, math.floor(risk / (atr_pct * mult)))))


def stop_distance(key: str, atr_pct: float, price: float) -> float | None:
    """The strategy's expected adverse move (ATR multiple × ATR) in price units — used as stop distance."""
    mult, _ = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    if not math.isfinite(atr_pct) or atr_pct <= 0 or not price > 0:
        return None
    return mult * atr_pct * price


def leverage_rule(key: str, cap: int, risk: float = RISK_PER_TYPICAL_MOVE) -> str:
    mult, own_max = LEVERAGE_PROFILE.get(key, DEFAULT_PROFILE)
    return f'Leverage: ⌊{risk:g} / (ATR% × {mult:g})⌋, 1× – {min(own_max, cap)}×.'


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
