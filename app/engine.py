"""Independent priority/radar lanes with bounded workers and immediate publication."""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any
import httpx
from .cache import AnswerCache
from .config import Settings
from .jev import Jev, JevError, PROMPT_VERSION
from .market import Market, demo_snapshot
from .strategy import technical, decide, research_levels
from .execution import Execution, pair_for
from .metrics import Metrics, event
from .store import Store
from .risk import finite

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, store: Store) -> None:
        self.settings, self.store = settings, store
        self.market, self.jev = Market(client), Jev(client, settings)
        self.execution = Execution(settings, client, store)
        self.rows: dict[str, dict[str, Any]] = {}
        self.ai_cache = AnswerCache(settings.ai_cache_size, settings.ai_cache_ttl,
                                    store if settings.ai_cache_persistent else None)
        self.metrics = Metrics()
        self.scanning = False
        self.last_scan = self.next_scan = None
        self.storage_error = False
        self.runtime_errors: set[str] = set()
        self.lock = asyncio.Lock()
        self.inflight: set[str] = set()
        self.symbols = settings.symbols if settings.symbols != ('ALL',) else ()
        self.top_volume: tuple[str, ...] = ()
        self.discovery_error = None
        self.scan_completed = 0
        self.priority_last_scan = None

    def _recover_storage(self) -> None:
        try:
            self.store.probe()
            self.storage_error = False
        except Exception as exc:
            self.storage_error = True
            event(log, logging.ERROR, 'storage_probe', error=exc)

    async def _batch(self, symbols: tuple[str, ...], parallelism: int, lane: str) -> None:
        # Only N tasks exist, even with thousands of symbols. Priority owns separate slots.
        queue = iter(symbols)
        started = time.monotonic()
        async def worker() -> None:
            for symbol in queue:
                await self._scan_symbol(symbol, lane=lane)
        await asyncio.gather(*(worker() for _ in range(min(parallelism, len(symbols)))))
        self.metrics.scan_seconds[lane] = round(time.monotonic() - started, 3)

    async def scan(self) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            self.scanning = True
            self._recover_storage()
            self.scan_completed = 0
            try:
                if self.settings.symbols == ('ALL',):
                    try:
                        self.symbols = (('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT')
                                        if self.settings.demo else await self.market.discover_symbols())
                        self.rows = {s: row for s, row in self.rows.items() if s in self.symbols}
                        self.market.prune(self.symbols)
                        self.discovery_error = None
                    except Exception as exc:
                        self.discovery_error = 'Bazar siyahısı yenilənmədi; yeni girişlər bloklandı.'
                        event(log, logging.WARNING, 'discovery', error=exc)
                try:
                    self.top_volume = self.symbols if self.settings.demo or self.settings.symbols != ('ALL',) else await self.market.volume_ranking(self.symbols)
                except Exception as exc:
                    self.top_volume = ()
                    event(log, logging.WARNING, 'volume_ranking', error=exc)
                await self._batch(self.symbols, self.settings.radar_parallelism, 'radar')
                self.last_scan = int(time.time() * 1000)
                self.next_scan = self.last_scan + self.settings.scan_seconds * 1000
            finally:
                self.scanning = False

    def priority_symbols(self) -> tuple[str, ...]:
        now = int(time.time()*1000)
        positions = self.execution.status().get('positions', [])
        opened = [p['pair'].replace('/USDT:USDT', 'USDT') for p in positions]
        recent = sorted((r for r in self.rows.values() if r.get('ai')
                         and 0 <= now-r['observed_at'] < self.settings.recent_confidence_seconds*1000
                         and r['ai']['answers']['direction']['choice'] in ('LONG','SHORT')
                         and r['ai']['answers']['direction']['confidence'] >= self.settings.min_confidence),
                        key=lambda r: r['observed_at'], reverse=True)
        recent_symbols = list(dict.fromkeys(r['symbol'] for r in recent if r['symbol'] not in opened))
        volume_symbols = [s for s in self.top_volume if s not in opened]
        size = self.settings.priority_size
        # Reserve space for both discovery sources; a sticky confident list cannot crowd out volume.
        candidates = list(dict.fromkeys(recent_symbols[:(size+1)//2] + volume_symbols[:size//2]
                                        + recent_symbols + volume_symbols))
        # Never truncate open positions; only the speculative watchlist is bounded.
        return tuple(dict.fromkeys(opened + candidates[:size]))

    def _key(self, snap: dict[str, Any], state: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps([PROMPT_VERSION, self.settings.model, state,
            {tf: f['close_time'] for tf, f in snap['frames'].items()}], sort_keys=True).encode()).hexdigest()

    def _state(self, snap: dict[str, Any], position: dict[str, Any] | None) -> dict[str, Any]:
        state = market_context(snap, self.settings)
        if position:
            state['position'] = dict(trade_id=position['trade_id'],
                direction='SHORT' if position['is_short'] else 'LONG',
                pnl_state='profit' if (position.get('profit_ratio') or 0) > 0 else 'loss_or_flat')
        return state

    async def _analyse(self, symbol: str) -> dict[str, Any]:
        snap = demo_snapshot(symbol) if self.settings.demo else await self.market.snapshot(symbol)
        ai, ai_error = None, None
        position = self.execution.position(symbol)
        if self.settings.demo:
            ai_error = 'DEMO: Jev sorğusu göndərilmir.'
        elif not self.settings.api_key:
            ai_error = 'API açarı yoxdur — Jev qərarı gözlənilir.'
        else:
            state = self._state(snap, position)
            key = self._key(snap, state)
            try:
                ai = self.ai_cache.get(key, bool(position))
                if ai is None:
                    ai = await self.jev.evaluate(state)
                    self.ai_cache.put(key, ai)
                # Recheck market + position after slow AI. Never re-date an old thesis blindly.
                updated = await self.market.snapshot(symbol)
                current_position = self.execution.position(symbol)
                if self._key(updated, self._state(updated, current_position)) != key:
                    ai, ai_error = None, 'Analiz zamanı bazar və ya mövqe dəyişdi; yenidən yoxlanacaq.'
                    self.metrics.counts['context_changed'] += 1
                snap, position = updated, current_position
            except JevError as exc:
                ai_error = str(exc)
                event(log, logging.WARNING, 'jev', symbol, exc)
        direction = ai['answers']['direction']['choice'] if ai else 'WAIT'
        tech = technical(snap, self.settings, direction)
        levels = research_levels(snap, direction, self.settings.risk)
        tech['guards'].append(dict(code='rr', label=f'Xərclər sonrası R:R ≥ {self.settings.risk.min_rr}',
            passed=direction == 'WAIT' or (levels is not None and levels['net_rr'] >= self.settings.risk.min_rr)))
        decision, reasons = decide(tech, ai, self.settings)
        codes = [g['code'] for g in tech['guards'] if not g['passed']]
        if not ai:
            codes.append('ai_unavailable')
        elif any(ai['answers'][k]['confidence'] < self.settings.min_confidence for k in ('direction','momentum','regime','risk')):
            codes.append('confidence')
        if direction == 'WAIT':
            codes.append('jev_wait')
        if decision == 'WAIT' and not codes:
            codes.append('jev_assessment')
        return {**snap, **tech, 'decision': decision, 'reasons': reasons, 'ai': ai, 'ai_error': ai_error,
                'levels': levels if decision != 'WAIT' else None, 'error': None,
                'position_id': position['trade_id'] if position else None, 'rejection_codes': codes}

    async def _scan_symbol(self, symbol: str, semaphore: asyncio.Semaphore | None = None,
                           lane: str = 'radar') -> None:
        if semaphore is not None:
            async with semaphore:
                await self._scan_symbol(symbol, lane=lane)
            return
        if symbol in self.inflight:
            self.metrics.counts['overlap_skipped'] += 1
            return
        self.inflight.add(symbol)
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(self.settings.symbol_timeout):
                    row = await self._analyse(symbol)
            except Exception as exc:
                event(log, logging.WARNING, 'analysis', symbol, exc)
                code = 'timeout' if isinstance(exc, TimeoutError) else 'analysis_error'
                self.metrics.counts[code] += 1
                row = dict(symbol=symbol, decision='WAIT', error='Bazar/AI analizi yoxlamadan keçmədi.',
                           observed_at=int(time.time()*1000), reasons=[code], rejection_code=code)
            row['analysis_ms'] = round((time.monotonic()-started)*1000, 2)
            row['generated_at'] = int(time.time()*1000)
            row['lane'] = lane
            try:
                self.store.append({k: v for k, v in row.items() if k != 'candles'})
            except Exception as exc:
                self.storage_error = True
                row.update(decision='WAIT', levels=None, error='Analiz diskə yazılmadı; icra bloklandı.')
                self.metrics.counts['storage_error'] += 1
                event(log, logging.ERROR, 'history_write', symbol, exc)
            self.rows[symbol] = row
            self.metrics.latencies.append(row['analysis_ms'])
            self.metrics.counts['analysed'] += 1
            self.metrics.counts['decision_' + row['decision']] += 1
            for code in row.get('rejection_codes', []):
                self.metrics.counts['reject_' + code] += 1
            if lane == 'radar':
                self.scan_completed += 1
        finally:
            self.inflight.discard(symbol)

    async def _priority_loop(self) -> None:
        while True:
            try:
                await self._batch(self.priority_symbols(), self.settings.priority_parallelism, 'priority')
                self.priority_last_scan = int(time.time()*1000)
                self.runtime_errors.discard('priority')
            except Exception as exc:
                self.runtime_errors.add('priority')
                event(log, logging.ERROR, 'priority_loop', error=exc)
            await asyncio.sleep(self.settings.priority_seconds)

    async def _radar_loop(self) -> None:
        while True:
            try:
                await self.scan()
                self.runtime_errors.discard('radar')
            except Exception as exc:
                self.runtime_errors.add('radar')
                event(log, logging.ERROR, 'radar_loop', error=exc)
            await asyncio.sleep(self.settings.scan_seconds)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._radar_loop())
            group.create_task(self._priority_loop())

    async def refresh_marks(self) -> None:
        """One shared bulk mark snapshot, never one blocking request per executor callback."""
        self.execution.marks = {}
        positions = self.execution.status().get('positions', [])
        if not positions or self.settings.demo:
            return
        try:
            async with asyncio.timeout(2):
                _, offset, _, premiums = await self.market._quotes()
            now = int(time.time()*1000)
            marks = {}
            for position in positions:
                pair = position['pair']
                symbol = pair.replace('/USDT:USDT', 'USDT')
                premium = premiums.get(symbol, {})
                price, observed = float(premium['markPrice']), int(premium['time'])-offset
                if not finite(price) or price <= 0 or not 0 <= now-observed <= 15000:
                    raise ValueError('Missing fresh position mark')
                marks[pair] = dict(price=price, observed_at=observed)
            self.execution.marks = marks
        except Exception as exc:
            event(log, logging.WARNING, 'position_marks', error=exc)

    def signals(self) -> dict[str, Any]:
        blocks = tuple(code for code, active in (('discovery', self.discovery_error),
                       ('worker_error', self.runtime_errors)) if active)
        return self.execution.signals(self.rows.values(), self.storage_error, blocks)

    def status(self) -> dict[str, Any]:
        now = int(time.time()*1000)
        bridge = self.signals()
        signals = {signal['pair']: signal for signal in bridge['signals']}
        rows = []
        for row in self.rows.values():
            stale = not 0 <= now-row['observed_at'] < self.settings.signal_ttl*1000
            signal = signals.get(pair_for(row['symbol']))
            execution_action = signal['action'] if signal else 'WAIT'
            execution_blocks = (signal.get('entry_blocks', []) if signal else
                                ['ttl' if stale else 'analysis_error' if row.get('error') or row.get('ai_error') else 'schema'])
            rows.append({**row, 'planned_leverage': signal.get('leverage_requested') if signal else None, 'execution_ready': execution_action in ('LONG', 'SHORT'),
                'execution_action': execution_action, 'execution_blocks': execution_blocks, 'stale': stale, 'decision': 'WAIT' if stale else row['decision'],
                'jev_decision': row.get('ai', {}).get('answers', {}).get('direction', {}).get('choice') if row.get('ai') else None,
                'analysis_age_seconds': max(0, (now-row['observed_at'])//1000), 'levels': None if stale else row.get('levels')})
        return dict(rows=rows, scanning=self.scanning, last_scan=self.last_scan, next_scan=self.next_scan,
            actionable_count=sum(signal['action'] in ('LONG', 'SHORT') for signal in bridge['signals']),
            market_count=len(self.symbols), market_symbols=self.symbols, scan_completed=self.scan_completed,
            discovery_error=self.discovery_error, demo=self.settings.demo, ai_configured=bool(self.settings.api_key),
            storage_error=self.storage_error, runtime_errors=sorted(self.runtime_errors), min_confidence=self.settings.min_confidence, model=self.settings.model,
            execution=self.execution.status(), metrics=self.metrics.status(), cache=self.ai_cache.stats(),
            priority_budget_exceeded=self.metrics.scan_seconds.get('priority', 0)+self.settings.priority_seconds >= self.settings.signal_ttl,
            performance_profile=self.settings.performance_profile, priority_symbols=self.priority_symbols(),
            priority_last_scan=self.priority_last_scan, bridge=bridge['diagnostics'])

def market_context(snap: dict[str, Any], settings: Settings) -> dict[str, Any]:
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
