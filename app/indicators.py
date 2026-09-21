"""Deterministic indicators. No AI arithmetic, no future bars."""
import math
from statistics import mean

INTERVALS = {'15m': 900_000, '1h': 3_600_000, '4h': 14_400_000}


def closed_bars(raw, interval, now):
    step = INTERVALS[interval]
    bars = []
    for r in raw:
        t, end = int(r[0]), int(r[6])
        if end >= now:
            continue
        o, h, l, c, v = map(float, r[1:6])
        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
            raise ValueError('Şam məlumatında etibarsız qiymət var.')
        if l > min(o, c) or h < max(o, c) or h < l or end != t + step - 1 or t % step:
            raise ValueError('Şam strukturu etibarsızdır.')
        bars.append(dict(time=t, close_time=end, open=o, high=h, low=l, close=c, volume=v))
    if len(bars) < 250:
        raise ValueError('Ən azı 250 bağlanmış şam lazımdır.')
    if any(b['time'] - a['time'] != step for a, b in zip(bars, bars[1:])):
        raise ValueError('Şam ardıcıllığında boşluq və ya təkrar var.')
    if now - bars[-1]['close_time'] > step + 30_000:
        raise ValueError('Bazar məlumatı köhnədir.')
    return bars


def ema(values, period):
    # SMA seed; None until the full seed is available.
    out = [None] * (period - 1)
    value = mean(values[:period])
    out.append(value)
    for x in values[period:]:
        value += 2 / (period + 1) * (x - value)
        out.append(value)
    return out


def wilder(values, period=14):
    value = mean(values[:period])
    for x in values[period:]:
        value = (value * (period - 1) + x) / period
    return value


def features(bars):
    c = [b['close'] for b in bars]
    e20, e50, e200 = [ema(c, p) for p in (20, 50, 200)]
    changes = [b - a for a, b in zip(c, c[1:])]
    gain = wilder([max(x, 0) for x in changes])
    loss = wilder([max(-x, 0) for x in changes])
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    tr = [max(b['high'] - b['low'], abs(b['high'] - a['close']), abs(b['low'] - a['close'])) for a, b in zip(bars, bars[1:])]
    atr = wilder(tr)
    fast, slow = ema(c, 12), ema(c, 26)
    macd = [f - s for f, s in zip(fast, slow) if s is not None]
    signal = ema(macd, 9)
    hist, previous = macd[-1] - signal[-1], macd[-2] - signal[-2]
    pivots_low, pivots_high = [], []
    recent = bars[-120:]
    for i in range(2, len(recent) - 2):
        window = recent[i - 2:i + 3]
        if all(recent[i]['low'] < b['low'] for j, b in enumerate(window) if j != 2):
            pivots_low.append(recent[i]['low'])
        if all(recent[i]['high'] > b['high'] for j, b in enumerate(window) if j != 2):
            pivots_high.append(recent[i]['high'])
    supports, resistances = [p for p in pivots_low if p < c[-1]], [p for p in pivots_high if p > c[-1]]
    avg_v = mean(b['volume'] for b in bars[-21:-1])
    trend = 'bullish' if c[-1] > e20[-1] > e50[-1] > e200[-1] else 'bearish' if c[-1] < e20[-1] < e50[-1] < e200[-1] else 'mixed'
    result = dict(close=c[-1], close_time=bars[-1]['close_time'], ema20=e20[-1], ema50=e50[-1], ema200=e200[-1],
                  rsi=rsi, atr=atr, atr_pct=atr / c[-1] * 100, macd=macd[-1], macd_signal=signal[-1],
                  macd_hist=hist, macd_change=hist - previous, relative_volume=bars[-1]['volume'] / avg_v if avg_v else 0,
                  support=max(supports) if supports else None, resistance=min(resistances) if resistances else None,
                  trend=trend, momentum='bullish' if hist > 0 and hist > previous else 'bearish' if hist < 0 and hist < previous else 'mixed',
                  volume_state='elevated' if avg_v and bars[-1]['volume'] >= 1.2 * avg_v else 'ordinary',
                  rsi_state='overbought' if rsi > 70 else 'oversold' if rsi < 30 else 'neutral')
    if any(isinstance(x, float) and not math.isfinite(x) for x in result.values()):
        raise ValueError('İndikator hesablaması sonlu deyil.')
    return result
