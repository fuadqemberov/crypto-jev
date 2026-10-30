"""30 fast scalping rules for 1-minute candles (scalping-5m branch).

Each rule maps an OHLCV frame to a 0/1 long position decided on the bar CLOSE; the engine acts at
the next price poll, mirrors the rule for shorts (app/futures.py) and closes every position after
at most MAX_HOLD_MINUTES (default 5) — or earlier at its stop, its 1.5R take-profit or the rule's
own exit signal. Parameters are short on purpose: a trade is meant to live for a few candles.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from .strategies import Strategy, cross_down, cross_up, regime, state


def _trend_up(df) -> pd.Series:
    return ind.ema(df.close, 20) > ind.ema(df.close, 50)


# --- trend / cross-overs -------------------------------------------------------------------
def sc_ema_5_13(df):
    return regime(ind.ema(df.close, 5) > ind.ema(df.close, 13))


def sc_ema_9_21(df):
    return regime((ind.ema(df.close, 9) > ind.ema(df.close, 21)) & (df.close > ind.ema(df.close, 50)))


def sc_macd_fast(df):
    line, sig, _ = ind.macd(df.close, 6, 13, 5)
    return regime(line > sig)


def sc_macd_zero(df):
    _, _, h = ind.macd(df.close)
    return state(cross_up(h, 0), cross_down(h, 0))


def sc_supertrend(df):
    return (ind.supertrend(df, 7, 2.0) == 1).astype(float)


def sc_tema(df):
    return regime(ind.tema(df.close, 5) > ind.tema(df.close, 15))


def sc_hma(df):
    h = ind.hma(df.close, 9)
    return regime(h > h.shift())


def sc_ichimoku_fast(df):
    tenkan, kijun, _, _ = ind.ichimoku(df, 6, 13, 26)
    return regime((tenkan > kijun) & (df.close > kijun))


def sc_adx7(df):
    a, p, m = ind.adx(df, 7)
    return state((a > 25) & cross_up(p, m), p < m)


def sc_psar_fast(df):
    sar = ind.parabolic_sar(df, 0.04, 0.4)
    return np.where(np.isnan(sar), 0.0, (df.close.to_numpy() > sar).astype(float))


def sc_heikin(df):
    ha_open, ha_close = ind.heikin_ashi(df)
    green = ha_close > ha_open
    return state(green & green.shift(fill_value=False) & ~green.shift(2, fill_value=True), ~green)


# --- breakouts / momentum ------------------------------------------------------------------
def sc_donchian10(df):
    return state(df.close > df.high.rolling(10).max().shift(), df.close < df.low.rolling(5).min().shift())


def sc_keltner(df):
    mid, a = ind.ema(df.close, 10), ind.atr(df, 10)
    return state(df.close > mid + 1.5 * a, df.close < mid)


def sc_bb_squeeze(df):
    lo, mid, up = ind.bollinger(df.close, 20, 2.0)
    width = (up - lo) / mid
    squeezed = (width <= width.rolling(50).min() * 1.1).shift(fill_value=False)
    return state(squeezed & (df.close > up), df.close < mid)


def sc_roc(df):
    r = ind.roc(df.close, 5)
    return state(cross_up(r, 0.15) & _trend_up(df), cross_down(r, 0))


def sc_volume_spike(df):
    spike = df.volume > 2.5 * ind.sma(df.volume, 20).shift()
    return state(spike & (df.close > df.open) & (df.close > df.high.shift()), df.close < df.low.shift())


def sc_inside_break(df):
    inside = (df.high.shift() < df.high.shift(2)) & (df.low.shift() > df.low.shift(2))
    return state(inside & (df.close > df.high.shift(2)), df.close < df.low.shift())


def sc_three_bar(df):
    c, lo = df.close, df.low
    up3 = (c > c.shift()) & (c.shift() > c.shift(2)) & (c.shift(2) > c.shift(3)) & (lo > lo.shift()) & (lo.shift() > lo.shift(2))
    return state(up3, c < c.shift())


def sc_vwap_cross(df):
    v = ind.daily_vwap(df)
    return state(cross_up(df.close, v) & _trend_up(df), df.close < v)


def sc_vwap_bounce(df):
    v = ind.daily_vwap(df)
    return state((df.low <= v) & (df.close > v) & _trend_up(df), cross_down(df.close, ind.ema(df.close, 20)))


# --- mean reversion / oscillators ----------------------------------------------------------
def sc_rsi7(df):
    r = ind.rsi(df.close, 7)
    return state(cross_up(r, 25), r > 60)


def sc_rsi2(df):
    r = ind.rsi(df.close, 2)
    return state((r < 10) & (df.close > ind.ema(df.close, 100)), r > 60)


def sc_stoch(df):
    k, d = ind.stochastic(df, 5, 3)
    return state(cross_up(k, d) & (k < 25), cross_down(k, d) | (k > 80))


def sc_bb_bounce(df):
    lo, mid, _ = ind.bollinger(df.close, 20, 2.0)
    return state(cross_up(df.close, lo), df.close >= mid)


def sc_cci(df):
    c = ind.cci(df, 14)
    return state(cross_up(c, -100), c > 100)


def sc_williams7(df):
    w = ind.williams_r(df, 7)
    return state(cross_up(w, -80), w > -20)


def sc_mfi7(df):
    m = ind.mfi(df, 7)
    return state(cross_up(m, 20), m > 70)


def sc_zscore(df):
    z = ind.zscore(df.close, 20)
    return state(cross_up(z, -2), z >= 0)


# --- candle patterns -----------------------------------------------------------------------
def sc_engulfing(df):
    o, c = df.open, df.close
    bull = (c > o) & (c.shift() < o.shift()) & (c >= o.shift()) & (o <= c.shift())
    return state(bull & _trend_up(df), c < df.low.shift())


def sc_hammer(df):
    o, c, h, lo = df.open, df.close, df.high, df.low
    body = (c - o).abs()
    lower = pd.concat([o, c], axis=1).min(axis=1) - lo
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    hammer = (lower >= 2 * body) & (upper <= body) & (h > lo)
    at_low = lo <= lo.rolling(20).min().shift()
    return state(hammer & at_low, c >= ind.ema(c, 20))


# key, name, family, rules, fn, ATR multiple (expected adverse move -> stop distance and leverage)
_RULES = [
    ('sc_ema_5_13', 'EMA 5/13 kəsişməsi', 'Trend', 'EMA5 EMA13-dən yuxarı keçəndə giriş, aşağı düşəndə çıxış.', sc_ema_5_13, 2.5),
    ('sc_ema_9_21', 'EMA 9/21 + EMA50 filtri', 'Trend', 'EMA9 > EMA21 və qiymət EMA50-dən yuxarı olanda giriş.', sc_ema_9_21, 2.5),
    ('sc_macd_fast', 'Sürətli MACD (6/13/5)', 'Momentum', 'Sürətli MACD xətti siqnal xəttini yuxarı keçəndə giriş.', sc_macd_fast, 2.5),
    ('sc_macd_zero', 'MACD histoqramı sıfır kəsişməsi', 'Momentum', 'Histoqram 0-ı yuxarı keçəndə giriş, aşağı keçəndə çıxış.', sc_macd_zero, 2.5),
    ('sc_supertrend', 'Supertrend (7, 2)', 'Trend', 'Sürətli Supertrend yuxarı dönəndə giriş.', sc_supertrend, 2.5),
    ('sc_tema', 'TEMA 5/15', 'Trend', 'TEMA5 TEMA15-dən yuxarı olanda mövqedə.', sc_tema, 2.5),
    ('sc_hma', 'Hull MA (9) yönü', 'Trend', 'HMA9 yuxarı dönəndə giriş, aşağı dönəndə çıxış.', sc_hma, 2.5),
    ('sc_ichimoku_fast', 'Sürətli İçimoku (6/13)', 'Trend', 'Tenkan > Kijun və qiymət Kijun-dan yuxarı olanda giriş.', sc_ichimoku_fast, 2.5),
    ('sc_adx7', 'ADX (7) + DI kəsişməsi', 'Trend', 'ADX > 25 ikən +DI −DI-ni yuxarı keçəndə giriş.', sc_adx7, 2.5),
    ('sc_psar_fast', 'Sürətli Parabolik SAR', 'Trend', 'Qiymət sürətli SAR (0.04 / 0.4) üstünə keçəndə giriş.', sc_psar_fast, 2.0),
    ('sc_heikin', 'Heikin-Ashi rəng dəyişməsi', 'Trend', 'Qırmızıdan sonra 2 yaşıl Heikin-Ashi şamı — giriş; ilk qırmızı — çıxış.', sc_heikin, 2.0),
    ('sc_donchian10', 'Donçian 10 qırılması', 'Qırılma', 'Son 10 şamın maksimumu qırılanda giriş, 5 şamın minimumunda çıxış.', sc_donchian10, 2.5),
    ('sc_keltner', 'Keltner (10, 1.5) qırılması', 'Qırılma', 'Qiymət EMA10 + 1.5×ATR üstündə bağlananda giriş, EMA10 altında çıxış.', sc_keltner, 2.5),
    ('sc_bb_squeeze', 'Bollinger sıxılma qırılması', 'Qırılma', 'Zolaq 50 şamın ən dar həddindədirsə və qiymət yuxarı zolağı qırırsa giriş.', sc_bb_squeeze, 2.5),
    ('sc_roc', 'ROC (5) impuls', 'Momentum', 'Trend yuxarı ikən 5 şamlıq dəyişmə +0.15%-i keçəndə giriş, 0-dan aşağı çıxış.', sc_roc, 2.0),
    ('sc_volume_spike', 'Həcm partlayışı', 'Həcm', 'Həcm orta həcmdən 2.5 qat çox, yaşıl şam və əvvəlki maksimum qırılıbsa giriş.', sc_volume_spike, 2.0),
    ('sc_inside_break', 'Inside-bar qırılması', 'Qırılma', 'Daxili şamdan sonra ana şamın maksimumu qırılanda giriş.', sc_inside_break, 2.0),
    ('sc_three_bar', '3 şamlıq impuls', 'Momentum', 'Ardıcıl 3 yüksələn bağlanış və yüksələn diblər — giriş; ilk aşağı bağlanış — çıxış.', sc_three_bar, 2.0),
    ('sc_vwap_cross', 'VWAP kəsişməsi', 'Həcm', 'Trend yuxarı ikən qiymət gündəlik VWAP-ı yuxarı keçəndə giriş, VWAP altında çıxış.', sc_vwap_cross, 2.0),
    ('sc_vwap_bounce', 'VWAP-dan sıçrayış', 'Həcm', 'Trend yuxarı ikən şam VWAP-a toxunub üstündə bağlananda giriş.', sc_vwap_bounce, 2.0),
    ('sc_rsi7', 'RSI (7) 25-dən dönüş', 'Orta qiymətə qayıdış', 'RSI7 25-i yuxarı keçəndə giriş, 60-dan yuxarı çıxış.', sc_rsi7, 2.0),
    ('sc_rsi2', 'RSI (2) ekstremum', 'Orta qiymətə qayıdış', 'RSI2 < 10 və qiymət EMA100-dən yuxarı — giriş; RSI2 > 60 — çıxış.', sc_rsi2, 2.0),
    ('sc_stoch', 'Stoxastik (5, 3)', 'Orta qiymətə qayıdış', '%K %D-ni 25-dən aşağıda yuxarı keçəndə giriş.', sc_stoch, 2.0),
    ('sc_bb_bounce', 'Bollinger alt zolaq sıçrayışı', 'Orta qiymətə qayıdış', 'Qiymət alt zolağı yuxarı keçəndə giriş, orta xətdə çıxış.', sc_bb_bounce, 2.0),
    ('sc_cci', 'CCI (14) −100 dönüşü', 'Orta qiymətə qayıdış', 'CCI −100-ü yuxarı keçəndə giriş, +100-dən yuxarı çıxış.', sc_cci, 2.0),
    ('sc_williams7', 'Williams %R (7)', 'Orta qiymətə qayıdış', '%R −80-i yuxarı keçəndə giriş, −20-dən yuxarı çıxış.', sc_williams7, 2.0),
    ('sc_mfi7', 'MFI (7)', 'Həcm', 'MFI7 20-ni yuxarı keçəndə giriş, 70-dən yuxarı çıxış.', sc_mfi7, 2.0),
    ('sc_zscore', 'Z-score (20) −2 dönüşü', 'Orta qiymətə qayıdış', 'Qiymət ortalamadan 2σ aşağıdan geri qayıdanda giriş, ortalamada çıxış.', sc_zscore, 2.0),
    ('sc_engulfing', 'Bullish engulfing', 'Şam modeli', 'Trend yuxarı ikən udan (engulfing) yaşıl şam — giriş; əvvəlki dibin altı — çıxış.', sc_engulfing, 2.0),
    ('sc_hammer', 'Çəkic (hammer) dibdə', 'Şam modeli', '20 şamın dibində çəkic şamı — giriş; EMA20-yə çatanda çıxış.', sc_hammer, 2.0),
]

STRATEGIES: list[Strategy] = [Strategy(k, n, f, r, fn) for k, n, f, r, fn, _ in _RULES]
ATR_MULT: dict[str, float] = {k: m for k, *_, m in _RULES}
