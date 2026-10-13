"""26 independent long-only spot strategies (20 classic + 6 YouTube tactics).

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


# --- 3 YouTube tactics (analysed 13 Oct 2026), reduced to what OHLCV alone can express ---------

def s_liq_sweep(df, n=20):
    """Liquidity / liquidation hunt (Doğan Çakır): price wicks under the 20-bar low where stops and
    long liquidations cluster, then closes back above it. Target = the opposite pool (20-bar high),
    stop = the sweep wick low. A liquidation heat-map is not in OHLCV, so swing extremes stand in."""
    low, high, close = df.low.to_numpy(), df.high.to_numpy(), df.close.to_numpy()
    pool_lo = df.low.rolling(n).min().shift().to_numpy()
    pool_hi = df.high.rolling(n).max().shift().to_numpy()
    pos = np.zeros(len(df))
    held, stop, target = False, 0.0, 0.0
    for i in range(len(df)):
        if held:
            if close[i] < stop or close[i] >= target:
                held = False
        elif not np.isnan(pool_lo[i]) and low[i] < pool_lo[i] < close[i] and pool_hi[i] > close[i]:
            held, stop, target = True, low[i], pool_hi[i]
        pos[i] = float(held)
    return pos


def s_macd_hist(df):
    """MACD histogram colours (video 2): two "dark green" bars in a row (histogram > 0 and rising)
    with price above the CM Williams Vix Fix support (low of the last green Vix Fix bar) -> long;
    the first "light green" bar (histogram falling), histogram <= 0 or a support break -> exit."""
    _, _, h = ind.macd(df.close)
    _, green = ind.vix_fix(df)
    support = df.low.where(green).ffill()
    dark = (h > 0) & (h > h.shift())
    entry = dark & dark.shift(fill_value=False) & (df.close > support)
    exit_ = (h < h.shift()) | (h <= 0) | (df.close < support)
    return state(entry, exit_)


def s_rr_2to1(df, stop=0.005, reward=2.0):
    """Fixed 2:1 risk/reward (video 3): "-10 % stop / +20 % take-profit at 20x" = -0.5 % / +1 % price.
    Entry: up-trend (EMA20 > EMA50) and close crosses back above the Bollinger middle band."""
    _, mid, _ = ind.bollinger(df.close)
    trend = ind.ema(df.close, 20) > ind.ema(df.close, 50)
    signal = (cross_up(df.close, mid) & trend).fillna(False).to_numpy()
    close = df.close.to_numpy()
    pos = np.zeros(len(df))
    held, lo, hi = False, 0.0, 0.0
    for i in range(len(df)):
        if held:
            if close[i] <= lo or close[i] >= hi:
                held = False
        elif signal[i]:
            held, lo, hi = True, close[i] * (1 - stop), close[i] * (1 + stop * reward)
        pos[i] = float(held)
    return pos


DAY_MS = 86_400_000
SESSION_OPEN_UTC = (0, 7)   # Asia (Tokyo) and London opens; New York is explicitly allowed in the video


def _near_session_open(index: pd.Index) -> np.ndarray:
    """True for bars starting within one hour before/after an Asia or London open (UTC)."""
    hour = (np.asarray(index, dtype='int64') // 3_600_000) % 24
    near = np.zeros(len(hour), dtype=bool)
    for h in SESSION_OPEN_UTC:
        near |= (hour == h) | (hour == (h - 1) % 24)
    return near


def s_ema_pullback(df, reward=2.0):
    """"Three rules" video: trade only with the EMA stack (EMA20 > EMA50 > SMA100), let the price
    come to you (a pull-back that touches EMA20 and closes back above it), stop just under EMA50,
    target 2R. No entries within 1h of the Asia/London opens, and once a coin is stopped out it
    is done for that UTC day (the daily loss limit); a target hit also ends the day (profit cap)."""
    e20, e50, m100 = ind.ema(df.close, 20), ind.ema(df.close, 50), ind.sma(df.close, 100)
    a = ind.atr(df, 14)
    stack = (e20 > e50) & (e50 > m100)
    signal = (stack & (df.low <= e20) & (df.close > e20)).fillna(False).to_numpy() & ~_near_session_open(df.index)
    close, e50v, av = df.close.to_numpy(), e50.to_numpy(), a.to_numpy()
    day = np.asarray(df.index, dtype='int64') // DAY_MS
    pos = np.zeros(len(df))
    held, stop, target, done_day = False, 0.0, 0.0, -1
    for i in range(len(df)):
        if held:
            if close[i] <= stop or close[i] >= target:
                held, done_day = False, day[i]
        elif signal[i] and day[i] != done_day and not np.isnan(av[i]):
            stop = min(e50v[i], close[i]) - 0.25 * av[i]
            if stop < close[i]:
                held, target = True, close[i] + reward * (close[i] - stop)
        pos[i] = float(held)
    return pos


def s_range_scalp(df, n=24):
    """Minute "channel" scalp video: in an up-trend (MA20 > MA50 > MA100) find the range of the last
    n bars; buy in its bottom quarter, take profit near the top, stop a little under the range low.
    A broken range ends the trade; the next range is measured fresh."""
    m20, m50, m100 = ind.sma(df.close, 20), ind.sma(df.close, 50), ind.sma(df.close, 100)
    hi = df.high.rolling(n).max().shift().to_numpy()
    lo = df.low.rolling(n).min().shift().to_numpy()
    a = ind.atr(df, 14).to_numpy()
    trend = ((m20 > m50) & (m50 > m100)).fillna(False).to_numpy()
    close = df.close.to_numpy()
    pos = np.zeros(len(df))
    held, stop, target = False, 0.0, 0.0
    for i in range(len(df)):
        if held:
            if close[i] <= stop or close[i] >= target:
                held = False
        elif trend[i] and not np.isnan(hi[i]) and not np.isnan(a[i]):
            width = hi[i] - lo[i]
            if width >= 2 * a[i] and lo[i] < close[i] <= lo[i] + 0.25 * width:
                held, stop, target = True, lo[i] - 0.25 * a[i], hi[i] - 0.1 * width
        pos[i] = float(held)
    return pos


def s_confluence(df):
    """"Çakır Osilatör" bot video: all five filters must agree (confidence 100) — price > MA50 > MA200,
    Supertrend up, ADX > 20, RSI 50–70, MACD histogram > 0 — and the entry waits for a pull-back to
    MA60 on the 1h chart (low touches MA60, close stays above). Exit when Supertrend turns down or
    the close falls under MA60."""
    m50, m60, m200 = ind.sma(df.close, 50), ind.sma(df.close, 60), ind.sma(df.close, 200)
    st = pd.Series(ind.supertrend(df, 10, 3.0), index=df.index)
    a, _, _ = ind.adx(df)
    r = ind.rsi(df.close, 14)
    _, _, h = ind.macd(df.close)
    score = (df.close > m50) & (m50 > m200) & (st == 1) & (a > 20) & (r > 50) & (r < 70) & (h > 0)
    entry = score & (df.low <= m60 * 1.003) & (df.close > m60)
    return state(entry, (st == -1) | (df.close < m60))


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
    Strategy('liq_sweep', 'Likvidasiya ovu (Doğan Çakır)', 'Likvidlik',
             'Şam son 20 şamın minimumunun altına fitil atıb (stop/likvidasiya ovu) yenidən onun üstündə bağlananda alış. '
             'Hədəf qarşı likvidlik — son 20 şamın maksimumu, stop — ov fitilinin dibi. Likvidasiya xəritəsi OHLCV-də olmadığı üçün swing ekstremumlar istifadə olunur; DCA yoxdur.',
             s_liq_sweep),
    Strategy('macd_hist', 'MACD histoqram rəngləri + Vix Fix', 'Momentum',
             'Histoqram ardıcıl 2 şam „tünd yaşıl“ (>0 və artan) və qiymət CM Williams Vix Fix dəstəyinin (son yaşıl Vix Fix şamının dibi) üstündədirsə alış; '
             'ilk „açıq yaşıl“ (histoqram azalır), histoqram ≤ 0 və ya dəstək qırılanda çıxış.',
             s_macd_hist),
    Strategy('rr_2to1', 'Sabit 2:1 risk/mükafat (−0.5% / +1%)', 'Trend',
             'EMA20 > EMA50 və qiymət Bollinger orta xəttini yuxarı kəsəndə alış. Stop girişdən −0.5%, hədəf +1% '
             '(20×-də marjanın −10% / +20%-i). Videodakı BTC istiqamət filtri tək coin məlumatında olmadığı üçün tətbiq edilmir.',
             s_rr_2to1),
    Strategy('ema_pullback', 'EMA geri çəkilmə + 3 qayda', 'Trend',
             'EMA20 > EMA50 > SMA100 sırasında qiymət EMA20-yə enib yenidən üstündə bağlananda alış („qiymət sənə gəlsin“). '
             'Stop EMA50-nin bir az altında, hədəf 2R. Asiya/London açılışından 1 saat əvvəl və sonra giriş yoxdur; '
             'coin həmin gün stop olarsa və ya hədəfə çatarsa, gün bitənə qədər yeni giriş yoxdur (gündəlik zərər/qazanc limiti).',
             s_ema_pullback),
    Strategy('range_scalp', 'Kanal scalp (kanalın dibində alış)', 'Orta qiymətə qayıdış',
             'MA20 > MA50 > MA100 trendində son 24 şamın kanalı (eni ≥ 2×ATR) götürülür; qiymət kanalın alt 25%-nə enəndə alış, '
             'hədəf kanalın yuxarısı (enin 90%-i), stop kanal minimumundan 0.25×ATR aşağı. Videoda 1 dəqiqəlik qrafikdir.',
             s_range_scalp),
    Strategy('confluence', 'Çakır Osilatör — 5 indikator təsdiqi + MA60', 'Trend',
             'Bütün şərtlər eyni vaxtda (güven xalı 100): qiymət > MA50 > MA200, Supertrend yuxarı, ADX > 20, RSI 50–70, MACD histoqramı > 0. '
             'Giriş MA60-a geri çəkilmədə (şamın dibi MA60-a toxunur, bağlanış üstündə). Supertrend aşağı dönəndə və ya MA60 altında çıxış.',
             s_confluence),
]
