"""Read-only Freqtrade telemetry and a fail-closed signal bridge. Never places orders."""
from __future__ import annotations
from collections.abc import Iterable
import httpx
from .config import Settings
from .store import Store
import asyncio
import hashlib
import math
import time
import logging
from collections import Counter
from typing import Any
from .bridge import VERSION, MAX_AGE_MS, PAIR
from .risk import finite, daily_loss_hit
from .metrics import event

log = logging.getLogger(__name__)


def pair_for(symbol: str) -> str:
    return symbol[:-4] + '/USDT:USDT'



class Execution:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, store: Store) -> None:
        self.settings, self.client, self.store = settings, client, store
        self.snapshot = {'connected': False, 'error': 'Freqtrade bağlantısı gözlənilir.'}
        self.heartbeat: dict[str, Any] = {}
        self.marks: dict[str, dict[str, float]] = {}
        self.last_pull = None
        self.failures = 0

    async def refresh(self) -> None:
        s = self.settings
        if not s.freqtrade_user:
            self.snapshot = {'connected': False, 'error': 'Freqtrade hələ konfiqurasiya edilməyib.'}
            return
        async def get(path: str) -> Any:
            r = await self.client.get(s.freqtrade_url.rstrip('/') + '/api/v1/' + path,
                                      auth=(s.freqtrade_user, s.freqtrade_password), timeout=3)
            r.raise_for_status()
            return r.json()
        try:
            config, trades, profit, balance, history = await asyncio.gather(
                *(get(path) for path in ('show_config', 'status', 'profit', 'balance', 'trades?limit=30')))
            if config.get('dry_run') is not True or config.get('strategy') != 'JevBridgeStrategy':
                raise ValueError('Only the paper bridge is accepted')
            if not isinstance(trades, list) or not isinstance(history.get('trades'), list):
                raise ValueError('Invalid telemetry')
            def public_trade(t: dict[str, Any]) -> dict[str, Any]:
                # Do not forward exchange config, API secrets or arbitrary upstream text.
                fields = ('trade_id', 'pair', 'is_short', 'open_rate', 'current_rate', 'close_rate',
                          'stake_amount', 'amount', 'leverage', 'profit_abs', 'profit_ratio',
                          'close_profit_abs', 'funding_fees', 'stop_loss_abs', 'open_timestamp',
                          'close_timestamp', 'is_open', 'exit_reason', 'enter_tag')
                return {k: t.get(k) for k in fields}
            usdt = next((c for c in balance.get('currencies', []) if c.get('currency') == 'USDT'), {})
            self.failures = 0
            self.snapshot = dict(connected=True, dry_run=True, observed_at=int(time.time() * 1000),
                error=None, state=config.get('state'), positions=[public_trade(t) for t in trades],
                trades=[public_trade(t) for t in history['trades']],
                wallet=usdt.get('balance'), free=usdt.get('free'), used=usdt.get('used'),
                realized_pnl=profit.get('profit_closed_coin'), total_pnl=profit.get('profit_all_coin'),
                wins=profit.get('winning_trades'), losses=profit.get('losing_trades'))
        except Exception as exc:
            self.failures += 1
            event(log, logging.WARNING, 'telemetry', error=exc)
            # Clear stale telemetry; never turn an API failure into a zero balance.
            self.snapshot = {'connected': False, 'error': 'Freqtrade məlumatı alınmadı və ya dry-run rejimi təsdiqlənmədi.'}

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(min(30, 3 * 2**min(self.failures, 4)))

    def receive_heartbeat(self, value: Any) -> None:
        now = int(time.time()*1000)
        if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('mode') != 'dry_run'
                or value.get('risk_policy') != self.settings.risk.identity
                or not finite(value.get('generated_at')) or not 0 <= now-value['generated_at'] <= MAX_AGE_MS
                or type(value.get('risk_ready')) is not bool or type(value.get('daily_loss_hit')) is not bool
                or not isinstance(value.get('cooldowns'), dict) or len(value['cooldowns']) > 5000
                or not isinstance(value.get('rejections'), dict) or len(value['rejections']) > 40):
            raise ValueError('Invalid executor heartbeat')
        for key in ('equity', 'realized_today', 'unrealized'):
            if not finite(value.get(key)):
                raise ValueError('Invalid executor risk telemetry')
        if value['daily_loss_hit'] != daily_loss_hit(value['equity'], value['realized_today'], value['unrealized'], self.settings.risk):
            raise ValueError('Inconsistent daily loss telemetry')
        for pair, until in value['cooldowns'].items():
            if not isinstance(pair, str) or not PAIR.fullmatch(pair) or not finite(until):
                raise ValueError('Invalid cooldown')
        for key, count in value['rejections'].items():
            if key not in {'bridge', 'ttl', 'confidence', 'pause', 'daily_loss', 'cooldown', 'duplicate',
                           'slippage', 'spread', 'rr', 'margin', 'telemetry', 'schema', 'risk_error', 'accepted'} or type(count) is not int or count < 0:
                raise ValueError('Invalid rejection counter')
        lag = value.get('bridge_lag_ms')
        if lag is not None and (not finite(lag) or lag < 0):
            raise ValueError('Invalid lag')
        self.heartbeat = {k: value[k] for k in ('version', 'generated_at', 'risk_ready', 'daily_loss_hit',
                         'equity', 'realized_today', 'unrealized', 'cooldowns', 'rejections')}
        self.heartbeat.update(received_at=now, bridge_lag_ms=lag)

    def status(self) -> dict[str, Any]:
        snap = self.snapshot
        now = int(time.time()*1000)
        fresh = snap.get('connected', False) and 0 <= now-snap['observed_at'] <= MAX_AGE_MS
        heartbeat_fresh = bool(self.heartbeat) and 0 <= now-self.heartbeat['generated_at'] <= MAX_AGE_MS
        try:
            paused, storage_ok = self.store.paused(), True
        except Exception as exc:
            paused, storage_ok = True, False
            event(log, logging.ERROR, 'controls_read', error=exc)
        return {**snap, 'connected': fresh, 'paused': paused, 'storage_ok': storage_ok,
                'bridge_configured': bool(self.settings.bridge_token), 'heartbeat_fresh': heartbeat_fresh,
                'executor_risk': self.heartbeat if heartbeat_fresh else None,
                'bridge_health': 'healthy' if fresh and heartbeat_fresh else 'unavailable',
                'last_pull_at': self.last_pull}

    def position(self, symbol: str) -> dict[str, Any] | None:
        status = self.status()
        if not status['connected']:
            return None
        return next((t for t in status['positions'] if t['pair'] == pair_for(symbol)), None)

    def signals(self, rows: Iterable[dict[str, Any]], storage_error: bool = False,
                external_blocks: tuple[str, ...] = ()) -> dict[str, Any]:
        now = int(time.time()*1000)
        status = self.status()
        risk = status.get('executor_risk') or {}
        blocks = list(external_blocks)
        for condition, code in ((not status['connected'] or status.get('state') != 'running', 'telemetry'),
                (not status['heartbeat_fresh'] or not risk.get('risk_ready'), 'heartbeat'),
                (status['paused'], 'pause'), (storage_error or not status['storage_ok'], 'storage_error'),
                (self.settings.demo, 'demo'), (risk.get('daily_loss_hit', True), 'daily_loss')):
            if condition:
                blocks.append(code)
        enabled = not blocks
        result = []
        rejections = Counter()
        for row in rows:
            observed = row.get('observed_at', 0)
            expires = observed + self.settings.signal_ttl*1000
            if not observed <= now < expires:
                rejections['ttl'] += 1
                continue
            if row.get('error') or row.get('ai_error') or not row.get('ai'):
                rejections['analysis_error'] += 1
                continue
            symbol, decision = row['symbol'], row['decision']
            if decision == 'WAIT':
                rejections.update(row.get('rejection_codes', ['decision_wait']))
            levels = row.get('levels')
            identity = f"{symbol}:{decision}:{row['frames']['15m']['close_time']}"
            signal = dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], pair=pair_for(symbol),
                          observed_at=observed, expires_at=expires, action='WAIT', levels=None)
            answer = row['ai']['answers'].get('leverage', {})
            requested, confidence = answer.get('choice'), answer.get('confidence')
            valid = (requested in {str(x) for x in (1,2,3,5,10,15,20,25,50,75,100)}
                     and finite(confidence) and confidence >= self.settings.leverage_confidence)
            cooling = now < risk.get('cooldowns', {}).get(signal['pair'], 0)
            if cooling:
                rejections['cooldown'] += 1
            if not valid:
                rejections['leverage_confidence'] += 1
            if enabled and not cooling and decision in ('LONG','SHORT') and levels and valid and all(
                    finite(levels.get(k)) and levels[k] > 0 for k in ('entry','stop','target')):
                funding = levels.get('funding_cost')
                if finite(funding) and funding >= 0:
                    signal.update(action=decision, leverage_requested=int(requested), funding_cost=funding,
                                  levels={k: levels[k] for k in ('entry','stop','target')})
                else:
                    rejections['funding'] += 1
            close = row['ai']['answers'].get('position_action', {})
            # Pause/daily entry guards never disable a fresh, position-bound JEV exit.
            if (status['connected'] and not storage_error and not external_blocks and status['storage_ok'] and not self.settings.demo
                    and close.get('choice') == 'CLOSE' and finite(close.get('confidence'))
                    and close['confidence'] >= self.settings.close_confidence and type(row.get('position_id')) is int):
                signal.update(close_trade_id=row['position_id'])
            result.append(signal)
        pairs = {s['pair'] for s in result if s['action'] in ('LONG','SHORT')}
        pairs.update(t['pair'] for t in status.get('positions', []) if t.get('pair'))
        return dict(version=VERSION, mode='dry_run', generated_at=now, risk_policy=self.settings.risk.identity,
                    entries_enabled=enabled, signals=result, marks=self.marks, pairs=sorted(pairs), refresh_period=self.settings.bridge_poll_seconds,
                    diagnostics=dict(blocks=blocks, rejections=dict(rejections), health=status['bridge_health'],
                                     bridge_lag_ms=risk.get('bridge_lag_ms'), executor_rejections=risk.get('rejections', {})))
