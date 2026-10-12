"""20 independent long-only spot strategies.

Each strategy maps an OHLCV frame to a 0/1 position array decided on the bar CLOSE.
The engine executes that decision at the NEXT bar open, so no future data is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str            # Azerbaijani display name
    family: str          # Trend / Momentum / Mean reversion / Breakout / Volume
    rules: str           # Azerbaijani rule description
    fn: Callable[[pd.DataFrame], np.ndarray]


def cross_up(a: pd.Series, b) -> pd.Series:
    b_prev = b.shift() if isinstance(b, pd.Series) else b
    return (a > b) & (a.shift() <= b_prev)


def cross_down(a: pd.Series, b) -> pd.Series:
    b_prev = b.shift() if isinstance(b, pd.Series) else b
    return (a < b) & (a.shift() >= b_prev)


def state(entry: pd.Series, exit_: pd.Series) -> np.ndarray:
    """Entry/exit events -> held position (exit wins if both fire on one bar)."""
    s = pd.Series(np.nan, index=entry.index)
    s[entry.fillna(False).to_numpy()] = 1.0
    s[exit_.fillna(False).to_numpy()] = 0.0
    return s.ffill().fillna(0.0).to_numpy()


def regime(cond: pd.Series) -> np.ndarray:
    return cond.fillna(False).astype(float).to_numpy()


def s_sma(df):
    return regime(ind.sma(df.close, 20) > ind.sma(df.close, 50))


def s_ema(df):
    return regime(ind.ema(df.close, 12) > ind.ema(df.close, 26))


def s_rsi(df):
    r = ind.rsi(df.close, 14)
    return state(cross_up(r, 30), cross_up(r, 70))


def s_macd(df):
    line, sig, _ = ind.macd(df.close)
    return regime(line > sig)


def s_bb_revert(df):
    lo, mid, _ = ind.bollinger(df.close)
    return state(df.close < lo, df.close > mid)


def s_bb_break(df):
    _, mid, up = ind.bollinger(df.close)
    return state(df.close > up, df.close < mid)


def s_stoch(df):
    k, d = ind.stochastic(df)
    return state(cross_up(k, d) & (k < 20), cross_down(k, d) & (k > 80))


def s_adx(df):
    a, p, m = ind.adx(df)
    return state((a > 25) & (p > m), (p < m) | (a < 20))


def s_ichimoku(df):
    tenkan, kijun, sa, sb = ind.ichimoku(df)
    top = pd.concat([sa, sb], axis=1).max(axis=1)
    return state((df.close > top) & (tenkan > kijun), df.close < kijun)


def s_vwap(df):
    vwap, trend = ind.daily_vwap(df), ind.ema(df.close, 200)
    return state(cross_up(df.close, vwap) & (df.close > trend), df.close < vwap)


def s_supertrend(df):
    return (ind.supertrend(df, 10, 3.0) == 1).astype(float)


def s_donchian(df):
    hi20, lo10 = df.high.rolling(20).max().shift(), df.low.rolling(10).min().shift()
    return state(df.close > hi20, df.close < lo10)


def s_williams(df):
    w = ind.williams_r(df, 14)
    return state(cross_up(w, -80), cross_down(w, -20))


def s_cci(df):
    c = ind.cci(df, 20)
    return state(cross_up(c, -100), cross_down(c, 100))


def s_psar(df):
    sar = ind.parabolic_sar(df)
    return np.where(np.isnan(sar), 0.0, (df.close.to_numpy() > sar).astype(float))


def s_keltner(df):
    mid, a = ind.ema(df.close, 20), ind.atr(df, 10)
    return state(df.close > mid + 2 * a, df.close < mid)


def s_atr_trail(df):
    """20-bar breakout entry, exit when close falls below a 3xATR trailing stop."""
    a = ind.atr(df, 14).to_numpy()
    close, high = df.close.to_numpy(), df.high.to_numpy()
    breakout = (df.close > df.high.rolling(20).max().shift()).to_numpy()
    pos = np.zeros(len(df))
    held, stop = False, 0.0
    for i in range(len(df)):
        if np.isnan(a[i]):
            continue
        if held:
            stop = max(stop, high[i] - 3 * a[i])
            if close[i] < stop:
                held = False
        elif breakout[i]:
            held, stop = True, close[i] - 3 * a[i]
        pos[i] = float(held)
    return pos


def s_tema(df):
    return regime(ind.tema(df.close, 10) > ind.tema(df.close, 30))


def s_mfi(df):
    m = ind.mfi(df, 14)
    return state(cross_up(m, 20), cross_down(m, 80))


def s_rsi_macd(df):
    r = ind.rsi(df.close, 14)
    line, sig, _ = ind.macd(df.close)
    return state(cross_up(line, sig) & (r > 50) & (r < 70), cross_down(line, sig) | (r > 78))


STRATEGIES: list[Strategy] = [
    Strategy('sma_cross', 'SMA kəsişməsi (20/50)', 'Trend', 'SMA20 SMA50-dən yuxarı olduqda alış mövqeyi, aşağı düşəndə çıxış.', s_sma),
    Strategy('ema_cross', 'EMA kəsişməsi (12/26)', 'Trend', 'EMA12 EMA26-dan yuxarı olduqda mövqedə qalır, əks kəsişmədə çıxır.', s_ema),
    Strategy('rsi', 'RSI həddindən artıq alış/satış', 'Orta qiymətə qayıdış', 'RSI(14) 30-u yuxarı kəsəndə alış, 70-i yuxarı kəsəndə satış.', s_rsi),
    Strategy('macd', 'MACD siqnal xətti kəsişməsi', 'Momentum', 'MACD(12,26,9) xətti siqnal xəttindən yuxarı olduqda mövqe açıqdır.', s_macd),
    Strategy('bb_revert', 'Bollinger orta qiymətə qayıdış', 'Orta qiymətə qayıdış', 'Qiymət aşağı Bollinger zolağından (20, 2σ) aşağı bağlananda alış, orta xəttə çatanda satış.', s_bb_revert),
    Strategy('bb_break', 'Bollinger qırılması', 'Qırılma', 'Qiymət yuxarı zolaqdan yuxarı bağlananda alış, orta xəttin altına düşəndə satış.', s_bb_break),
    Strategy('stoch', 'Stoxastik osilyator', 'Momentum', '%K %D-ni 20-dən aşağıda yuxarı kəsəndə alış, 80-dən yuxarıda aşağı kəsəndə satış.', s_stoch),
    Strategy('adx', 'ADX trend izləmə', 'Trend', 'ADX>25 və +DI>−DI olduqda alış; +DI<−DI və ya ADX<20 olduqda çıxış.', s_adx),
    Strategy('ichimoku', 'İçimoku buludu', 'Trend', 'Qiymət buluddan yuxarı və Tenkan>Kijun olduqda alış, Kijun altına düşəndə satış.', s_ichimoku),
    Strategy('vwap', 'VWAP strategiyası', 'Həcm', 'Gündəlik VWAP yuxarı kəsilir və qiymət EMA200-dən yuxarıdırsa alış, VWAP altında çıxış.', s_vwap),
    Strategy('supertrend', 'Supertrend (10, 3)', 'Trend', 'Supertrend yüksələn rejimdə olduqda mövqe açıqdır.', s_supertrend),
    Strategy('donchian', 'Donçian kanal qırılması', 'Qırılma', 'Son 20 şamın maksimumu qırılanda alış, son 10 şamın minimumu qırılanda satış.', s_donchian),
    Strategy('williams', 'Williams %R', 'Orta qiymətə qayıdış', '%R −80-i yuxarı kəsəndə alış, −20-ni aşağı kəsəndə satış.', s_williams),
    Strategy('cci', 'CCI (Əmtəə Kanal İndeksi)', 'Orta qiymətə qayıdış', 'CCI(20) −100-ü yuxarı kəsəndə alış, +100-ü aşağı kəsəndə satış.', s_cci),
    Strategy('psar', 'Parabolik SAR', 'Trend', 'Qiymət SAR nöqtələrindən yuxarı olduqda mövqe açıqdır.', s_psar),
    Strategy('keltner', 'Keltner kanalı', 'Qırılma', 'Qiymət EMA20 + 2×ATR(10) üst sərhədini qıranda alış, EMA20 altına düşəndə satış.', s_keltner),
    Strategy('atr_trail', 'ATR izləyən stop', 'Qırılma', '20 şamlıq maksimum qırılanda alış, 3×ATR(14) izləyən stop qırılanda satış.', s_atr_trail),
    Strategy('tema', 'Üçqat EMA (TEMA 10/30)', 'Trend', 'TEMA10 TEMA30-dan yuxarı olduqda mövqe açıqdır.', s_tema),
    Strategy('mfi', 'Pul axını indeksi (MFI)', 'Həcm', 'MFI(14) 20-ni yuxarı kəsəndə alış, 80-i aşağı kəsəndə satış.', s_mfi),
    Strategy('rsi_macd', 'RSI + MACD kombinasiyası', 'Momentum', 'MACD yuxarı kəsişməsi və RSI 50–70 arasında olduqda alış; MACD aşağı kəsişməsi və ya RSI>78 olduqda satış.', s_rsi_macd),
]
