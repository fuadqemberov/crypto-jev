"""Vectorized technical indicators on pandas Series. Every value at bar t uses bars <= t only."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df.close.shift()
    return pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    gain, loss = rma(d.clip(lower=0), n), rma(-d.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).where(loss != 0, 100.0).where(gain.notna())


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3):
    lo, hi = df.low.rolling(n).min(), df.high.rolling(n).max()
    k = 100 * (df.close - lo) / (hi - lo).replace(0, np.nan)
    return k, sma(k, d)


def adx(df: pd.DataFrame, n: int = 14):
    up, down = df.high.diff(), -df.low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), n)
    plus_di, minus_di = 100 * rma(plus_dm, n) / tr, 100 * rma(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, n), plus_di, minus_di


def ichimoku(df: pd.DataFrame, t: int = 9, k: int = 26, b: int = 52):
    mid = lambda n: (df.high.rolling(n).max() + df.low.rolling(n).min()) / 2  # noqa: E731
    tenkan, kijun = mid(t), mid(k)
    # Senkou spans are plotted k bars ahead, so the cloud at bar t was known at t-k.
    span_a, span_b = ((tenkan + kijun) / 2).shift(k), mid(b).shift(k)
    return tenkan, kijun, span_a, span_b


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored at each UTC day start."""
    day = (df.index.values // 86_400_000)
    tp = (df.high + df.low + df.close) / 3
    pv = (tp * df.volume).groupby(day).cumsum()
    vol = df.volume.groupby(day).cumsum()
    return (pv / vol.replace(0, np.nan)).fillna(df.close)


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> np.ndarray:
    """Returns +1 (up-trend) / -1 (down-trend) per bar."""
    a = atr(df, n).to_numpy()
    hl2 = ((df.high + df.low) / 2).to_numpy()
    close = df.close.to_numpy()
    upper, lower = hl2 + mult * a, hl2 - mult * a
    trend = np.zeros(len(df))
    fu, fl, direction = np.nan, np.nan, 1
    for i in range(len(df)):
        if np.isnan(a[i]):
            continue
        if np.isnan(fu):
            fu, fl = upper[i], lower[i]
        else:
            fu = upper[i] if (upper[i] < fu or close[i - 1] > fu) else fu
            fl = lower[i] if (lower[i] > fl or close[i - 1] < fl) else fl
        if direction == 1 and close[i] < fl:
            direction = -1
        elif direction == -1 and close[i] > fu:
            direction = 1
        trend[i] = direction
    return trend


def parabolic_sar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> np.ndarray:
    high, low = df.high.to_numpy(), df.low.to_numpy()
    sar = np.full(len(df), np.nan)
    if len(df) < 2:
        return sar
    up, af, ep, s = True, step, high[0], low[0]
    for i in range(1, len(df)):
        s = s + af * (ep - s)
        if up:
            s = min(s, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < s:
                up, s, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            s = max(s, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > s:
                up, s, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)
        sar[i] = s
    return sar


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hi, lo = df.high.rolling(n).max(), df.low.rolling(n).min()
    return -100 * (hi - df.close) / (hi - lo).replace(0, np.nan)


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df.high + df.low + df.close) / 3
    m = sma(tp, n)
    md = pd.Series(np.nan, index=df.index)
    if len(tp) >= n:
        win = np.lib.stride_tricks.sliding_window_view(tp.to_numpy(), n)
        md.iloc[n - 1:] = np.abs(win - win.mean(axis=1, keepdims=True)).mean(axis=1)
    return (tp - m) / (0.015 * md.replace(0, np.nan))


def tema(s: pd.Series, n: int) -> pd.Series:
    e1 = ema(s, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return 3 * e1 - 3 * e2 + e3


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tp = (df.high + df.low + df.close) / 3
    flow = tp * df.volume
    diff = tp.diff()
    pos = flow.where(diff > 0, 0.0).rolling(n).sum()
    neg = flow.where(diff < 0, 0.0).rolling(n).sum()
    ratio = pos / neg.replace(0, np.nan)
    return (100 - 100 / (1 + ratio)).where(neg != 0, 100.0).where(pos.notna())
