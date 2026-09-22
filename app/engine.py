import asyncio
import hashlib
import json
import logging
import time
from .jev import Jev, JevError, PROMPT_VERSION
from .market import Market, demo_snapshot
from .strategy import technical, decide, research_levels
from .execution import Execution

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings, client, store):
        self.settings, self.store = settings, store
        self.market, self.jev = Market(client), Jev(client, settings)
        self.execution = Execution(settings, client, store)
        self.rows, self.ai_cache = {}, {}
        self.scanning = False
        self.last_scan = None
        self.next_scan = None
        self.storage_error = False
        self.lock = asyncio.Lock()

    async def scan(self):
        if self.lock.locked():
            return
        async with self.lock:
            self.scanning = True
            try:
                self.storage_error = False
                semaphore = asyncio.Semaphore(2)
                await asyncio.gather(*(self._scan_symbol(symbol, semaphore) for symbol in self.settings.symbols))
                self.last_scan = int(time.time() * 1000)
                self.next_scan = self.last_scan + self.settings.scan_seconds * 1000
            finally:
                self.scanning = False

    async def _scan_symbol(self, symbol, semaphore):
        async with semaphore:
            try:
                snap = demo_snapshot(symbol) if self.settings.demo else await self.market.snapshot(symbol)
                ai, ai_error = None, None
                position = self.execution.position(symbol)
                if self.settings.demo:
                    ai_error = 'DEMO: Jev sorğusu göndərilmir.'
                elif not self.settings.api_key:
                    ai_error = 'API açarı yoxdur — Jev qərarı gözlənilir.'
                else:
                    state = market_context(snap, self.settings)
                    if position:
                        state['position'] = dict(trade_id=position['trade_id'],
                            direction='SHORT' if position['is_short'] else 'LONG',
                            pnl_state='profit' if (position.get('profit_ratio') or 0) > 0 else 'loss_or_flat')
                    cache_key = hashlib.sha256(json.dumps([PROMPT_VERSION, self.settings.model, state,
                        {tf: f['close_time'] for tf, f in snap['frames'].items()}], sort_keys=True).encode()).hexdigest()
                    try:
                        if cache_key not in self.ai_cache:
                            self.ai_cache[cache_key] = await self.jev.evaluate(state)
                            if len(self.ai_cache) > 200:
                                del self.ai_cache[next(iter(self.ai_cache))]
                        ai = self.ai_cache[cache_key]
                    except JevError as e:
                        ai_error = str(e)
                # Jev owns direction. Python may veto but never reverse it.
                direction = ai['answers']['direction']['choice'] if ai else 'WAIT'
                tech = technical(snap, self.settings, direction)
                levels = research_levels(snap, direction)
                tech['guards'].append(dict(label='Komissiya/slippage sonrası R:R ≥ 1.5', passed=direction == 'WAIT' or (levels is not None and levels['net_rr'] >= 1.5)))
                decision, reasons = decide(tech, ai, self.settings)
                row = {**snap, **tech, 'decision': decision, 'reasons': reasons, 'ai': ai, 'ai_error': ai_error,
                       'levels': levels if decision != 'WAIT' else None, 'error': None,
                       'position_id': position['trade_id'] if position else None}
            except Exception:
                # Never return raw upstream bodies, URLs, credentials or stale signals.
                log.warning('Market analysis failed for %s', symbol)
                row = dict(symbol=symbol, decision='WAIT', error='Bazar məlumatı alınmadı və ya yoxlamadan keçmədi.',
                           observed_at=int(time.time() * 1000), reasons=['Etibarlı təzə məlumat yoxdur.'])
            try:
                self.store.append({k: v for k, v in row.items() if k != 'candles'})
            except Exception:
                self.storage_error = True
                row.update(decision='WAIT', levels=None, error='Analiz diskə yazılmadı; icra bloklandı.')
                log.error('Analysis history could not be saved')
            self.rows[symbol] = row

    async def run(self):
        while True:
            await self.scan()
            await asyncio.sleep(self.settings.scan_seconds)

    def status(self):
        now = int(time.time() * 1000)
        rows = []
        for row in self.rows.values():
            ttl = self.settings.signal_ttl if self.settings.bridge_token else self.settings.scan_seconds + 60
            stale = not 0 <= now - row['observed_at'] < ttl * 1000
            rows.append({**row, 'stale': stale, 'decision': 'WAIT' if stale else row['decision'],
                         'levels': None if stale else row.get('levels')})
        return dict(rows=rows, scanning=self.scanning, last_scan=self.last_scan, next_scan=self.next_scan,
                    demo=self.settings.demo, ai_configured=bool(self.settings.api_key), storage_error=self.storage_error,
                    min_confidence=self.settings.min_confidence, model=self.settings.model,
                    execution=self.execution.status())


def market_context(snap, settings):
    """Jev receives semantic market context; all comparisons stay in Python."""
    frames = {}
    for tf, f in snap['frames'].items():
        atr = f['atr']
        distance = abs(f['close'] - f['ema20']) / atr if atr > 0 else 999
        frames[tf] = {k: f[k] for k in ('trend', 'momentum', 'volume_state', 'rsi_state')}
        frames[tf].update(
            macd_direction='bullish' if f['macd_hist'] > 0 else 'bearish' if f['macd_hist'] < 0 else 'flat',
            rsi_momentum='bullish' if f['rsi'] > 55 else 'bearish' if f['rsi'] < 45 else 'neutral',
            extension='extreme' if distance > 3 else 'extended' if distance > 2 else 'ordinary',
            price_vs_ema20='above' if f['close'] > f['ema20'] else 'below' if f['close'] < f['ema20'] else 'at',
            volatility='extreme' if f['atr_pct'] > 5 else 'quiet' if f['atr_pct'] < .1 else 'ordinary',
            support='unknown' if f['support'] is None else 'near' if f['close'] - f['support'] <= atr else 'distant',
            resistance='unknown' if f['resistance'] is None else 'near' if f['resistance'] - f['close'] <= atr else 'distant')
    return dict(symbol=snap['symbol'], horizon='1 to 4 hours', frames=frames,
                market_quality={'spread': 'pass' if snap['spread_bps'] <= settings.max_spread else 'fail',
                                'funding_extreme': abs(snap['funding_rate']) > .001,
                                'long_funding_cost': 'high' if snap['funding_rate'] > settings.max_funding else 'ordinary',
                                'short_funding_cost': 'high' if -snap['funding_rate'] > settings.max_funding else 'ordinary',
                                'price_gap': 'fail' if abs(snap['mark'] - snap['frames']['15m']['close']) > snap['frames']['15m']['atr'] else 'pass'},
                unavailable=['news', 'order-book depth', 'on-chain data'])
