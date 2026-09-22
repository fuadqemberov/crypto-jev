"""TypeSafe's documented System One HTTP protocol, not Chat Completions."""
import asyncio
import math
import httpx


def choice(instructions, criteria):
    return dict(type='choice', instructions=instructions, criteria=criteria)


QUESTIONS = {
    'direction': choice('You are the primary decision-maker for a crypto research signal with a 1-4 hour horizon. Evaluate the supplied multi-timeframe market context. Choose the most defensible directional thesis, or WAIT when evidence is weak, conflicting or an entry is extended. Trend continuation, a supported pullback and a plausible reversal may each be valid; no technical-score threshold determines your answer. Use the precomputed semantic labels; do not calculate prices or invent news. This is a research assessment, not a claim of future returns.', {
        'LONG': 'The supplied price structure, momentum and participation support an upward thesis with a reasonable entry context.',
        'SHORT': 'The supplied price structure, momentum and participation support a downward thesis with a reasonable entry context.',
        'WAIT': 'Neither thesis is persuasive, the evidence conflicts materially, or the entry context is poor.'}),
    'momentum': choice('Assess the dominant momentum from the 15m and 1h context, treating 15m as timing and 1h as confirmation. Use momentum, MACD direction and RSI labels. Select mixed when neither side has convincing support.', {
        'bullish': 'The balance of evidence supports upward momentum.', 'bearish': 'The balance of evidence supports downward momentum.',
        'mixed': 'Momentum is indecisive or materially contradictory.'}),
    'regime': choice('Classify the current market structure using all supplied timeframe, structure and participation labels. This is a context assessment, not a directional recommendation.', {
        'trend': 'A directional trend is established.', 'range': 'Price is moving without a clear sustained direction.',
        'transition': 'Evidence suggests a trend change or emerging breakout.', 'unclear': 'The regime cannot be established.'}),
    'risk': choice('Assess entry-context risk from supplied market_quality, extension, volatility and participation labels. A known failed market-quality check, extreme volatility or extreme extension is elevated risk. Ordinary disagreement alone can be acceptable for a supported reversal. Do not assume missing support/resistance means no barrier.', {
        'acceptable': 'The context has no material entry-risk warning and enough evidence to assess it.',
        'elevated': 'The context contains a material entry-risk warning.',
        'unclear': 'Evidence is insufficient to assess entry-context risk.'}),
    'driver': choice('Identify the single most influential feature of the supplied market context. Choose only an evidence category that is actually present. This independently assessed driver is diagnostic, not free-form reasoning.', {
        'trend_alignment': 'Directional trends align across timeframes.',
        'momentum_shift': 'Momentum is changing relative to the prevailing trend.',
        'volume_support': 'Elevated participation supports the move.',
        'structure_test': 'Price is close to a confirmed support or resistance.',
        'overextension': 'Price is extended from its moving average or RSI is extreme.',
        'conflicting_evidence': 'Conflicting or insufficient evidence dominates.'}),
}
POSITION_QUESTIONS = {
    'position_action': choice('Assess the supplied existing position against the current market context. Choose CLOSE only when the thesis is invalidated or reversal risk justifies exiting. Choose HOLD when evidence is insufficient or the thesis remains intact. Never calculate prices, change leverage or remove a stoploss.', {
        'HOLD': 'Keep the existing position under its independent protective stop.',
        'CLOSE': 'Exit the existing position because its thesis has materially deteriorated.'}),
}
PROMPT_VERSION = '3'



class JevError(Exception):
    pass


def parse_response(data, questions=None):
    questions = questions or QUESTIONS
    if not isinstance(data, dict) or not isinstance(data.get('model'), str) or not data['model']:
        raise JevError('Jev cavabında model yoxdur.')
    answers = data.get('answers')
    if not isinstance(answers, dict):
        raise JevError('Jev cavabı etibarsızdır.')
    for name, question in questions.items():
        a = answers.get(name)
        if not isinstance(a, dict) or a.get('type') != 'choice' or a.get('choice') not in question['criteria']:
            raise JevError('Jev qərar strukturu etibarsızdır.')
        p = a.get('probabilities')
        confidence = a.get('confidence')
        valid = lambda x: type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
        if not valid(confidence) or not isinstance(p, dict) or set(p) != set(question['criteria']) or not all(valid(v) for v in p.values()):
            raise JevError('Jev ehtimalları etibarsızdır.')
        if abs(sum(p.values()) - 1) > .001 or p[a['choice']] < max(p.values()) - .000001:
            raise JevError('Jev ehtimal bölgüsü uyğunsuzdur.')
    # Whitelist fields so upstream text cannot leak into logs/UI or state.
    return dict(model=data['model'], answers={k: {field: answers[k][field] for field in ('type', 'choice', 'confidence', 'probabilities')} for k in questions})


class Jev:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    async def evaluate(self, state):
        if not self.settings.api_key:
            raise JevError('TYPESAFE_API_KEY təyin edilməyib.')
        questions = {**QUESTIONS, **POSITION_QUESTIONS} if state.get('position') else QUESTIONS
        for attempt in range(3):
            try:
                r = await self.client.post('https://api.typesafe.ai/v1/systemone',
                    headers={'Authorization': 'Bearer ' + self.settings.api_key},
                    json={'model': self.settings.model, 'state': state, 'questions': questions})
                if r.status_code in (429, 529, 502, 503, 504) and attempt < 2:
                    # Honor Retry-After; abandon this scan rather than retry too early.
                    try:
                        retry = float(r.headers.get('Retry-After', str(2 ** (attempt + 1))))
                    except ValueError:
                        retry = 30
                    if not math.isfinite(retry) or retry > 30:
                        raise JevError('Jev limiti: sorğu sonrakı skana saxlanıldı.')
                    await asyncio.sleep(max(2 ** (attempt + 1), retry))
                    continue
                if r.status_code in (401, 403):
                    raise JevError('Jev API açarı və ya giriş icazəsi etibarsızdır.')
                if r.is_error:
                    raise JevError(f'Jev xidməti HTTP {r.status_code} qaytardı.')
                return parse_response(r.json(), questions)
            except (httpx.HTTPError, ValueError) as e:
                raise JevError('Jev cavabı alınmadı; siqnal WAIT olaraq saxlanıldı.') from e
        raise JevError('Jev sorğu limiti.')
