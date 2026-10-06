"""Independent priority/radar lanes with bounded workers and immediate publication.

With SYMBOLS=ALL the radar no longer analyses every market: the RSI heatmap picks the candidates
(app.heatmap) and only those plus open positions get a full analysis.
"""
import asyncio
import logging
import time
from typing import Any
import httpx
from .config import Settings
from .market import Market, demo_snapshot
from .heatmap import Heatmap
from .strategy import evaluate, STRATEGY_ID
from .execution import Execution, pair_for
from .metrics import Metrics, event
from .store import Store
from .risk import finite

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, store: Store) -> None:
        self.settings, self.store = settings, store
        self.market = Market(client)
        self.heatmap = Heatmap(settings, client, self.market)
        self.execution = Execution(settings, client, store)
        self.rows: dict[str, dict[str, Any]] = {}
        self.metrics = Metrics()
        self.scanning = False
        self.last_scan = self.next_scan = None
        self.storage_error = False
        self.runtime_errors: set[str] = set()
        self.lock = asyncio.Lock()
        self.inflight: set[str] = set()
        self.symbols = settings.symbols if settings.symbols != ('ALL',) else ()
        # Ranked candidates for the priority lane: heatmap order, or 24h volume when the heatmap fails.
        self.watchlist: tuple[str, ...] = ()
        self.radar_symbols: tuple[str, ...] = ()
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
                self.radar_symbols = await self._radar_selection()
                await self._batch(self.radar_symbols, self.settings.radar_parallelism, 'radar')
                self.last_scan = int(time.time() * 1000)
                self.next_scan = self.last_scan + self.settings.scan_seconds * 1000
            finally:
                self.scanning = False

    def _open_symbols(self) -> list[str]:
        return [p['pair'].replace('/USDT:USDT', 'USDT') for p in self.execution.status().get('positions', [])]

    async def _radar_selection(self) -> tuple[str, ...]:
        if self.settings.demo or self.settings.symbols != ('ALL',):
            self.watchlist = self.symbols
            return self.symbols
        try:
            await self.heatmap.refresh(self.symbols)
            self.watchlist = self.heatmap.select()
        except Exception:
            try:
                self.watchlist = (await self.market.volume_ranking(self.symbols))[:self.settings.heatmap_size]
            except Exception as exc:
                self.watchlist = ()
                event(log, logging.WARNING, 'volume_ranking', error=exc)
        # Open positions are always re-analysed so exits never depend on the heatmap.
        return tuple(dict.fromkeys(self._open_symbols() + list(self.watchlist)))

    def priority_symbols(self) -> tuple[str, ...]:
        now = int(time.time()*1000)
        opened = self._open_symbols()
        recent = sorted((r for r in self.rows.values()
                         if r.get('candidate') in ('LONG', 'SHORT')
                         and 0 <= now-r['observed_at'] < self.settings.recent_signal_seconds*1000),
                        key=lambda r: r['observed_at'], reverse=True)
        recent_symbols = list(dict.fromkeys(r['symbol'] for r in recent if r['symbol'] not in opened))
        volume_symbols = [s for s in self.watchlist if s not in opened]
        size = self.settings.priority_size
        # Reserve space for both discovery sources; a sticky confident list cannot crowd out volume.
        candidates = list(dict.fromkeys(recent_symbols[:(size+1)//2] + volume_symbols[:size//2]
                                        + recent_symbols + volume_symbols))
        # Never truncate open positions; only the speculative watchlist is bounded.
        return tuple(dict.fromkeys(opened + candidates[:size]))

    async def _analyse(self, symbol: str) -> dict[str, Any]:
        snap = demo_snapshot(symbol) if self.settings.demo else await self.market.snapshot(symbol)
        position = self.execution.position(symbol)
        result = evaluate(snap, self.settings, position)
        return {**snap, **result, 'error': 'Bazar məlumatı yoxlanmadı.' if result['rejection_codes'] == ['market_data'] else None,
                'position_id': position['trade_id'] if position else None}

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
                row = dict(symbol=symbol, decision='WAIT', error='Bazar analizi yoxlamadan keçmədi.',
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
                                ['ttl' if stale else 'analysis_error' if row.get('error') else 'schema'])
            rows.append({**row, 'planned_leverage': signal.get('leverage_requested') if signal else None, 'execution_ready': execution_action in ('LONG', 'SHORT'),
                'execution_action': execution_action, 'execution_blocks': execution_blocks, 'stale': stale, 'decision': 'WAIT' if stale else row['decision'],
                'analysis_age_seconds': max(0, (now-row['observed_at'])//1000), 'levels': None if stale else row.get('levels')})
        return dict(rows=rows, scanning=self.scanning, last_scan=self.last_scan, next_scan=self.next_scan,
            actionable_count=sum(signal['action'] in ('LONG', 'SHORT') for signal in bridge['signals']),
            market_count=len(self.symbols), market_symbols=self.symbols, scan_completed=self.scan_completed,
            radar_count=len(self.radar_symbols), radar_symbols=self.radar_symbols, heatmap=self.heatmap.status(),
            discovery_error=self.discovery_error, demo=self.settings.demo, strategy=STRATEGY_ID,
            storage_error=self.storage_error, runtime_errors=sorted(self.runtime_errors),
            execution=self.execution.status(), metrics=self.metrics.status(),
            priority_budget_exceeded=self.metrics.scan_seconds.get('priority', 0)+self.settings.priority_seconds >= self.settings.signal_ttl,
            performance_profile=self.settings.performance_profile, priority_symbols=self.priority_symbols(),
            priority_last_scan=self.priority_last_scan, bridge=bridge['diagnostics'])
