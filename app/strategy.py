"""Heuristic research rules, not a calibrated win probability."""

def technical(snapshot, settings, ai_direction=None):
    frames = snapshot['frames']
    f = frames['15m']
    direction = ai_direction or 'WAIT'
    bullish = direction == 'LONG'
    trend = 'bullish' if bullish else 'bearish'
    sign = 1 if bullish else -1
    rules = []
    def rule(label, points, passed):
        rules.append(dict(label=label, maximum=points, points=points if passed else 0, passed=bool(passed)))
    for tf in ('15m', '1h', '4h'):
        rule(tf + ' EMA trend uyğunluğu', 15, direction != 'WAIT' and frames[tf]['trend'] == trend)
    rule('15m MACD güclənməsi', 15, direction != 'WAIT' and f['macd_hist'] * sign > 0 and f['macd_change'] * sign > 0)
    rule('1h MACD istiqaməti', 10, direction != 'WAIT' and frames['1h']['macd_hist'] * sign > 0)
    rule('15m RSI momentum', 10, (50 <= f['rsi'] <= 70 if bullish else 30 <= f['rsi'] <= 50) and direction != 'WAIT')
    rule('1h RSI momentum', 10, (50 <= frames['1h']['rsi'] <= 70 if bullish else 30 <= frames['1h']['rsi'] <= 50) and direction != 'WAIT')
    rule('Nisbi həcm ≥ 1.2', 10, f['relative_volume'] >= 1.2)
    guards = []
    def guard(label, passed):
        guards.append(dict(label=label, passed=bool(passed)))
    guard('ATR / qiymət 0.1–5%', .1 <= f['atr_pct'] <= 5)
    guard('Cari qiymət siqnaldan ≤ 1 ATR', abs(snapshot['mark'] - f['close']) <= f['atr'])
    guard('Spread limiti', snapshot['spread_bps'] <= settings.max_spread)
    guard('Funding limiti (ödəniş istiqaməti)', abs(snapshot['funding_rate']) <= .001 and snapshot['funding_rate'] * sign <= settings.max_funding)
    guard('RSI həddindən artıq deyil', 22 <= f['rsi'] <= 78)
    score = sum(r['points'] for r in rules)
    return dict(candidate=direction, score=score, rules=rules, guards=guards)


def decide(result, ai, settings):
    reasons = [g['label'] for g in result['guards'] if not g['passed']]
    if result['candidate'] == 'WAIT':
        reasons.append('Jev WAIT seçib və ya hələ qərar verməyib.')
    if ai is None:
        reasons.append('Jev təsdiqi yoxdur.')
    else:
        a = ai['answers']
        if any(a[k]['confidence'] < settings.min_confidence for k in ('direction', 'momentum', 'regime', 'risk')):
            reasons.append('Jev confidence həddindən aşağıdır.')
        if a['direction']['choice'] != result['candidate']:
            reasons.append('Jev qərarı ilə istifadə olunan istiqamət uyğun deyil.')
        if a['momentum']['choice'] != ('bullish' if result['candidate'] == 'LONG' else 'bearish'):
            reasons.append('Jev momentum təsdiqi yoxdur.')
        if a['regime']['choice'] == 'unclear' or a['risk']['choice'] != 'acceptable':
            reasons.append('Jev rejim/risk filtri keçilmədi.')
    return ('WAIT' if reasons else result['candidate']), reasons


def research_levels(snapshot, direction):
    if direction == 'WAIT':
        return None
    sign = 1 if direction == 'LONG' else -1
    entry = snapshot['ask'] if sign == 1 else snapshot['bid']
    risk = 2 * snapshot['frames']['15m']['atr']
    stop, target = entry - sign * risk, entry + sign * 2 * risk
    if min(stop, target) <= 0 or risk <= 0:
        return None
    # Indicative round-trip taker fee 0.05% per side + slippage 3bps per side.
    cost_to_target = .0008 * (entry + target)
    cost_to_stop = .0008 * (entry + stop)
    return dict(entry=entry, stop=stop, target=target, gross_rr=2.,
                net_rr=(2 * risk - cost_to_target) / (risk + cost_to_stop),
                note='İndikativ plan; Freqtrade əlavə icra yoxlaması tətbiq edir. Funding R:R hesabına daxil deyil.')
