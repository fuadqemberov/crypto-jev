"""Consensus branch: the live top 5 (15–20 Oct 2026) plus a 3-of-5 consensus — 6 accounts.

Each of the five MEMBERS trades its own account with its own orders. The sixth account, ``consensus3``,
opens one order when at least CONSENSUS_MIN (3) of the five hold the same direction on the same coin. Each rule maps an OHLCV frame to a 0/1 position array decided on the bar
CLOSE; the engine acts at the next bar, so no future data is used. The other 21 rules live on master.
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


def s_adx(df):
    a, p, m = ind.adx(df)
    return state((a > 25) & (p > m), (p < m) | (a < 20))


def s_williams(df):
    w = ind.williams_r(df, 14)
    return state(cross_up(w, -80), cross_down(w, -20))


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


MEMBERS: list[Strategy] = [
    Strategy('ema_pullback', 'EMA geri çəkilmə + 3 qayda', 'Trend',
             'EMA20 > EMA50 > SMA100 sırasında qiymət EMA20-yə enib yenidən üstündə bağlananda alış („qiymət sənə gəlsin“). '
             'Stop EMA50-nin bir az altında, hədəf 2R. Asiya/London açılışından 1 saat əvvəl və sonra giriş yoxdur; '
             'coin həmin gün stop olarsa və ya hədəfə çatarsa, gün bitənə qədər yeni giriş yoxdur (gündəlik zərər/qazanc limiti).',
             s_ema_pullback),
    Strategy('williams', 'Williams %R', 'Orta qiymətə qayıdış', '%R −80-i yuxarı kəsəndə alış, −20-ni aşağı kəsəndə satış.', s_williams),
    Strategy('adx', 'ADX trend izləmə', 'Trend', 'ADX>25 və +DI>−DI olduqda alış; +DI<−DI və ya ADX<20 olduqda çıxış.', s_adx),
    Strategy('rr_2to1', 'Sabit 2:1 risk/mükafat (−0.5% / +1%)', 'Trend',
             'EMA20 > EMA50 və qiymət Bollinger orta xəttini yuxarı kəsəndə alış. Stop girişdən −0.5%, hədəf +1% '
             '(20×-də marjanın −10% / +20%-i). Videodakı BTC istiqamət filtri tək coin məlumatında olmadığı üçün tətbiq edilmir.',
             s_rr_2to1),
    Strategy('confluence', 'Çakır Osilatör — 5 indikator təsdiqi + MA60', 'Trend',
             'Bütün şərtlər eyni vaxtda (güven xalı 100): qiymət > MA50 > MA200, Supertrend yuxarı, ADX > 20, RSI 50–70, MACD histoqramı > 0. '
             'Giriş MA60-a geri çəkilmədə (şamın dibi MA60-a toxunur, bağlanış üstündə). Supertrend aşağı dönəndə və ya MA60 altında çıxış.',
             s_confluence),
]
CONSENSUS_MIN = 3


def member_votes(df) -> np.ndarray:
    """Per bar: how many of the five members hold a long on this frame (on the mirrored frame: a short)."""
    return np.sum([np.nan_to_num(np.asarray(m.fn(df), dtype=float)) > 0.5 for m in MEMBERS], axis=0)


def s_consensus(df):
    """Long while at least CONSENSUS_MIN of the five members are long on this coin (any three); the
    position ends when fewer than two still agree, or at stop / TP. The futures layer applies the same
    rule to the mirrored series, which gives the short side: >= 3 members short."""
    votes = pd.Series(member_votes(df), index=df.index)
    return state(votes >= CONSENSUS_MIN, votes <= 1)


CONSENSUS = Strategy(
    'consensus3', 'Konsensus 3/5 (top 5-dən ən azı 3-ü)', 'Konsensus',
    'Analiz: EMA geri çəkilmə, Williams %R, ADX, Sabit 2:1 və Çakır Osilatör. İstənilən ən azı 3-ü eyni coin-də eyni '
    'istiqamətdə (LONG və ya SHORT) olanda bir order açılır; 2-dən az strategiya razı qalanda, stop və ya TP-də bağlanır.',
    s_consensus)
STRATEGIES: list[Strategy] = [*MEMBERS, CONSENSUS]   # 6 accounts: the five trade alone + the consensus
