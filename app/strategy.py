"""Versioned, deterministic closed-candle trend and continuation rules."""
from __future__ import annotations
import time
from typing import Any
from .config import Settings, EntryRules
from .indicators import INTERVALS
from .risk import RiskPolicy, adverse_funding, net_rr, finite

STRATEGY_ID = 'trend-continuation-v2'


def evaluate(snapshot: dict[str, Any], settings: Settings,
             position: dict[str, Any] | None = None, *, now: int | None = None) -> dict[str, Any]:
    """Require aligned higher-timeframe trend and a 15m reclaim, pullback or breakout.

    Missing, nonfinite, stale, or inconsistent inputs invalidate entries AND discretionary
    exits. Executor-side fixed stops, targets and maximum hold remain independent.
    """
    now = int(time.time()*1000) if now is None else now
    invalid = dict(strategy=STRATEGY_ID, candidate='WAIT', decision='WAIT', exit_action='HOLD',
                   levels=None, guards=[], reasons=['Bazar məlumatı etibarsız və ya köhnədir.'],
                   rejection_codes=['market_data'])
    try:
        if not finite(now) or not finite(snapshot['observed_at']) or not 0 <= now-snapshot['observed_at'] <= 15000:
            return invalid
        if not all(finite(snapshot[k]) for k in ('bid', 'ask', 'mark', 'spread_bps', 'funding_rate')):
            return invalid
        if not 0 < snapshot['bid'] <= snapshot['ask'] or snapshot['mark'] <= 0 or snapshot['spread_bps'] < 0:
            return invalid
        frames = snapshot['frames']
        for tf, step in INTERVALS.items():
            f = frames[tf]
            required = ('open', 'high', 'low', 'previous_high', 'previous_low', 'close', 'previous_close', 'previous_ema20', 'ema20', 'ema50', 'ema200',
                        'ema50_previous', 'atr', 'atr_pct',
                        'rsi', 'macd_hist', 'macd_change', 'relative_volume', 'close_time')
            if not all(finite(f[k]) for k in required):
                return invalid
            if (min(f[k] for k in ('open', 'high', 'low', 'previous_high', 'previous_low', 'close', 'previous_close', 'previous_ema20', 'ema20', 'ema50', 'ema200', 'atr')) <= 0
                    or not 0 <= f['rsi'] <= 100 or f['relative_volume'] < 0
                    or not 0 < now-f['close_time'] <= step+30000):
                return invalid
            if (f['low'] > min(f['open'], f['close']) or f['high'] < max(f['open'], f['close'])
                    or f['high'] < f['low'] or f['previous_high'] < f['previous_low']):
                return invalid
        f, hourly, slow = (frames[t] for t in ('15m', '1h', '4h'))
        def trend(frame: dict[str, Any], sign: int, *, confirm_fast: bool = False) -> bool:
            # Long-term EMA200 ordering often lags a genuine trend reversal by days.
            # Keep the 4h EMA50 slope and price regime, and confirm 1h with EMA20.
            return (sign*(frame['close']-frame['ema50']) > 0
                    and sign*(frame['ema50']-frame['ema50_previous']) > 0
                    and (not confirm_fast or sign*(frame['ema20']-frame['ema50']) > 0))
        sign = 1 if trend(hourly, 1, confirm_fast=True) and trend(slow, 1) else -1 if trend(hourly, -1, confirm_fast=True) and trend(slow, -1) else 0
        candidate = 'LONG' if sign == 1 else 'SHORT' if sign == -1 else 'WAIT'
        rules = settings.rules
        guards: list[dict[str, Any]] = []
        def guard(code: str, label: str, passed: bool) -> None:
            guards.append(dict(code=code, label=label, passed=bool(passed)))
        guard('trend', '1s və 4s trend rejimi və EMA50 meyli eyni istiqamətdədir', sign != 0)
        reclaim = sign*(f['previous_close']-f['previous_ema20']) <= 0 and sign*(f['close']-f['ema20']) > 0
        pullback = (sign*(f['close']-f['ema20']) > 0 and sign*(f['close']-f['open']) > 0
                    and sign*(f['low']-f['ema20']) <= .25*f['atr'] if sign == 1 else
                    sign*(f['close']-f['ema20']) > 0 and sign*(f['close']-f['open']) > 0
                    and sign*(f['high']-f['ema20']) <= .25*f['atr'])
        breakout = (sign*(f['close']-(f['previous_high'] if sign == 1 else f['previous_low'])) > 0)
        setup = 'reclaim' if reclaim else 'pullback' if pullback else 'breakout' if breakout else 'none'
        guard('setup', '15d reclaim, təsdiqli EMA20 pullback və ya 20 şam breakout', setup != 'none')
        momentum_ok = sign*f['macd_hist'] > 0 and (setup != 'breakout' or sign*f['macd_change'] > 0)
        guard('momentum', 'MACD histogramı trend istiqamətindədir; breakout-da güclənir', momentum_ok)
        rsi_low = 45 if sign == 1 else 100-rules.rsi_long_max
        rsi_high = rules.rsi_long_max if sign == 1 else 55
        guard('rsi', 'RSI trendi təsdiqləyir, həddən artıq deyil', rsi_low <= f['rsi'] <= rsi_high if sign else False)
        volume_floor = max(rules.min_relative_volume, 1.0) if setup == 'breakout' else rules.min_relative_volume
        guard('volume', f'Həcm / əvvəlki 20 şam ≥ {volume_floor:g}', f['relative_volume'] >= volume_floor)
        guard('extension', f'EMA20-dən uzaqlıq ≤ {rules.max_extension_atr} ATR', abs(f['close']-f['ema20']) <= rules.max_extension_atr*f['atr'])
        guard('volatility', f'ATR / qiymət {rules.min_atr_pct}–{rules.max_atr_pct}%', rules.min_atr_pct <= f['atr_pct'] <= rules.max_atr_pct)
        guard('price_gap', f'Cari qiymət bağlanmış şamdan ≤ {rules.max_price_gap_atr} ATR', abs(snapshot['mark']-f['close']) <= rules.max_price_gap_atr*f['atr'])
        spread = (snapshot['ask']-snapshot['bid'])/((snapshot['ask']+snapshot['bid'])/2)*10000
        guard('spread', 'Spread limiti', max(spread, snapshot['spread_bps']) <= settings.max_spread)
        guard('funding', 'Funding limiti', abs(snapshot['funding_rate']) <= .001 and sign*snapshot['funding_rate'] <= settings.max_funding)
        levels = research_levels(snapshot, candidate, settings.risk, rules)
        guard('rr', f'Xərclər sonrası R:R ≥ {settings.risk.min_rr}', levels is not None and levels['net_rr'] >= settings.risk.min_rr)
        failed = [g for g in guards if not g['passed']]
        # Exit requires a closed 1h EMA50 break and confirming 15m momentum, not a fresh entry setup.
        exit_action = 'HOLD'
        if position is not None and type(position.get('is_short')) is bool:
            held_sign = -1 if position['is_short'] else 1
            if held_sign*(hourly['close']-hourly['ema50']) < 0 and held_sign*f['macd_hist'] < 0:
                exit_action = 'CLOSE'
        return dict(strategy=STRATEGY_ID, candidate=candidate, setup=setup, decision='WAIT' if failed else candidate,
                    exit_action=exit_action, guards=guards, levels=None if failed else levels,
                    reasons=[g['label'] for g in failed], rejection_codes=[g['code'] for g in failed])
    except (KeyError, TypeError, ValueError, OverflowError):
        return invalid


def research_levels(snapshot: dict[str, Any], direction: str, policy: RiskPolicy | None = None, rules: EntryRules | None = None) -> dict[str, Any] | None:
    policy = policy or RiskPolicy()
    rules = rules or EntryRules()
    if direction not in ('LONG', 'SHORT'):
        return None
    sign = 1 if direction == 'LONG' else -1
    entry = snapshot['ask'] if sign == 1 else snapshot['bid']
    distance = rules.stop_atr*snapshot['frames']['15m']['atr']
    stop, target = entry-sign*distance, entry+sign*rules.target_r*distance
    if not all(finite(v) and v > 0 for v in (entry, stop, target, distance)):
        return None
    funding = adverse_funding(snapshot['funding_rate'], direction, policy.funding_periods)
    return dict(entry=entry, stop=stop, target=target, gross_rr=rules.target_r,
                net_rr=net_rr(entry, stop, target, funding, policy), funding_cost=funding,
                note=f'{rules.stop_atr} ATR sabit stop; {rules.target_r}R hədəf; komissiya, slippage və funding nəzərə alınıb.')
