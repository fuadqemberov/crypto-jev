"""Read-only Freqtrade telemetry and a fail-closed signal bridge. Never places orders."""
import asyncio
import hashlib
import math
import time


def pair_for(symbol):
    return symbol[:-4] + '/USDT:USDT'


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class Execution:
    def __init__(self, settings, client, store):
        self.settings, self.client, self.store = settings, client, store
        self.snapshot = {'connected': False, 'error': 'Freqtrade bağlantısı gözlənilir.'}

    async def refresh(self):
        s = self.settings
        if not s.freqtrade_user:
            self.snapshot = {'connected': False, 'error': 'Freqtrade hələ konfiqurasiya edilməyib.'}
            return
        async def get(path):
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
            def public_trade(t):
                # Do not forward exchange config, API secrets or arbitrary upstream text.
                fields = ('trade_id', 'pair', 'is_short', 'open_rate', 'current_rate', 'close_rate',
                          'stake_amount', 'amount', 'leverage', 'profit_abs', 'profit_ratio',
                          'close_profit_abs', 'funding_fees', 'stop_loss_abs', 'open_timestamp',
                          'close_timestamp', 'is_open', 'exit_reason', 'enter_tag')
                return {k: t.get(k) for k in fields}
            usdt = next((c for c in balance.get('currencies', []) if c.get('currency') == 'USDT'), {})
            self.snapshot = dict(connected=True, dry_run=True, observed_at=int(time.time() * 1000),
                error=None, state=config.get('state'), positions=[public_trade(t) for t in trades],
                trades=[public_trade(t) for t in history['trades']],
                wallet=usdt.get('balance'), free=usdt.get('free'), used=usdt.get('used'),
                realized_pnl=profit.get('profit_closed_coin'), total_pnl=profit.get('profit_all_coin'),
                wins=profit.get('winning_trades'), losses=profit.get('losing_trades'))
        except Exception:
            # Clear stale telemetry; never turn an API failure into a zero balance.
            self.snapshot = {'connected': False, 'error': 'Freqtrade məlumatı alınmadı və ya dry-run rejimi təsdiqlənmədi.'}

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(5)

    def status(self):
        snap = self.snapshot
        fresh = snap.get('connected', False) and 0 <= int(time.time()*1000) - snap['observed_at'] <= 15000
        return {**snap, 'connected': fresh, 'paused': self.store.paused(),
                'bridge_configured': bool(self.settings.bridge_token)}

    def position(self, symbol):
        status = self.status()
        if not status['connected']:
            return None
        return next((t for t in status['positions'] if t['pair'] == pair_for(symbol)), None)

    def signals(self, rows, storage_error=False):
        now = int(time.time() * 1000)
        status = self.status()
        enabled = (status['connected'] and status.get('state') == 'running'
                   and not status['paused'] and not storage_error and not self.settings.demo)
        result = []
        for row in rows:
            observed = row.get('observed_at', 0)
            expires = observed + self.settings.signal_ttl * 1000
            if row.get('error') or row.get('ai_error') or not observed <= now < expires or not row.get('ai'):
                continue
            symbol, decision = row['symbol'], row['decision']
            levels = row.get('levels')
            # One entry per symbol/direction/closed 15m candle, even across restarts.
            identity = f"{symbol}:{decision}:{row['frames']['15m']['close_time']}"
            signal = dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], pair=pair_for(symbol),
                          observed_at=observed, expires_at=expires, action='WAIT', levels=None)
            if enabled and decision in ('LONG', 'SHORT') and levels and all(
                    finite(levels.get(k)) and levels[k] > 0 for k in ('entry', 'stop', 'target')):
                signal.update(action=decision, levels={k: levels[k] for k in ('entry', 'stop', 'target')})
            position_answer = row['ai']['answers'].get('position_action', {})
            if (position_answer.get('choice') == 'CLOSE'
                    and position_answer.get('confidence', 0) >= self.settings.min_confidence
                    and row.get('position_id') is not None):
                # Exits remain available during entry pause or entry risk veto.
                signal.update(close_trade_id=row['position_id'])
            result.append(signal)
        return dict(version=1, mode='dry_run', generated_at=now, entries_enabled=enabled, signals=result)
