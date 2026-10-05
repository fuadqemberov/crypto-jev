"""Non-directional vetoes; JEV alone supplies LONG/SHORT/WAIT."""
from __future__ import annotations
from typing import Any
from .config import Settings
from .risk import RiskPolicy, adverse_funding, net_rr, finite

def technical(snapshot: dict[str, Any], settings: Settings, ai_direction: str | None = None) -> dict[str, Any]:
    frames = snapshot['frames']
    f = frames['15m']
    direction = ai_direction or 'WAIT'
    bullish = direction == 'LONG'
    sign = 1 if bullish else -1
    guards = []
    def guard(label: str, passed: bool, code: str) -> None:
        guards.append(dict(label=label, passed=bool(passed), code=code))
    guard('ATR / qiymət 0.1–5%', .1 <= f['atr_pct'] <= 5, 'volatility')
    guard('Cari qiymət siqnaldan ≤ 1 ATR', abs(snapshot['mark'] - f['close']) <= f['atr'], 'price_gap')
    guard('Spread limiti', snapshot['spread_bps'] <= settings.max_spread, 'spread')
    guard('Funding limiti (ödəniş istiqaməti)', abs(snapshot['funding_rate']) <= .001 and snapshot['funding_rate'] * sign <= settings.max_funding, 'funding')
    guard('RSI həddindən artıq deyil', 22 <= f['rsi'] <= 78, 'rsi')
    return dict(candidate=direction, guards=guards)


def decide(result: dict[str, Any], ai: dict[str, Any] | None, settings: Settings) -> tuple[str, list[str]]:
    if result.get('candidate') not in ('LONG', 'SHORT', 'WAIT') or not isinstance(result.get('guards'), list):
        return 'WAIT', ['Qərar yoxlaması etibarsızdır.']
    reasons = [g['label'] for g in result['guards'] if not g['passed']]
    if result['candidate'] == 'WAIT':
        reasons.append('Jev WAIT seçib və ya hələ qərar verməyib.')
    if ai is None:
        reasons.append('Jev təsdiqi yoxdur.')
    else:
        a = ai.get('answers', {})
        if not isinstance(a, dict) or any(not isinstance(a.get(k), dict) or not finite(a[k].get('confidence'))
                or not 0 <= a[k]['confidence'] <= 1 for k in ('direction', 'momentum', 'regime', 'risk')):
            return 'WAIT', reasons + ['Jev confidence məlumatı etibarsızdır.']
        # Entry assessments and CLOSE/leverage have independent confidence thresholds.
        if any(a[k]['confidence'] < settings.min_confidence for k in ('direction', 'momentum', 'regime', 'risk')):
            reasons.append('Jev confidence həddindən aşağıdır.')
        if a['direction'].get('choice') != result['candidate']:
            reasons.append('Jev qərarı ilə istifadə olunan istiqamət uyğun deyil.')
        if a['momentum'].get('choice') != ('bullish' if result['candidate'] == 'LONG' else 'bearish'):
            reasons.append('Jev momentum təsdiqi yoxdur.')
        if a['regime'].get('choice') not in ('trend', 'range', 'transition') or a['risk'].get('choice') != 'acceptable':
            reasons.append('Jev rejim/risk filtri keçilmədi.')
    return ('WAIT' if reasons else result['candidate']), reasons


def research_levels(snapshot: dict[str, Any], direction: str, policy: RiskPolicy | None = None) -> dict[str, Any] | None:
    policy = policy or RiskPolicy()
    if direction == 'WAIT':
        return None
    sign = 1 if direction == 'LONG' else -1
    entry = snapshot['ask'] if sign == 1 else snapshot['bid']
    risk = 2 * snapshot['frames']['15m']['atr']
    stop, target = entry - sign * risk, entry + sign * 2 * risk
    if min(stop, target) <= 0 or risk <= 0:
        return None
    funding = adverse_funding(snapshot['funding_rate'], direction, policy.funding_periods)
    return dict(entry=entry, stop=stop, target=target, gross_rr=2.,
                net_rr=net_rr(entry, stop, target, funding, policy), funding_cost=funding,
                note='Komissiya, slippage və mümkün mənfi funding ehtiyatı daxil edilib.')
