"""Freqtrade 2026.8 paper executor. JEV is the only directional signal source."""
import math
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

log = logging.getLogger(__name__)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def signal_tag(signal):
    levels = signal['levels']
    return f"jev:{signal['id']}:{levels['stop']:.12g}:{levels['target']:.12g}:{signal['leverage_requested']}"


def trade_plan(trade):
    """The entry plan lives in Freqtrade's persisted enter_tag, not a volatile cache."""
    try:
        parts = trade.enter_tag.split(':')
        if len(parts) not in (4, 5):
            return None
        prefix, identity, stop, target = parts[:4]
        stop, target = float(stop), float(target)
        if prefix == 'jev' and identity and all(number(v) and v > 0 for v in (stop, target)):
            return stop, target
    except (AttributeError, TypeError, ValueError):
        pass
    return None


class JevBridgeStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '1m'
    can_short = True
    process_only_new_candles = False
    startup_candle_count = 2
    minimal_roi = {}
    stoploss = -0.50  # Margin fallback. Leverage cap keeps the ATR plan inside this distance.
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    position_adjustment_enable = False
    trailing_stop = False

    @property
    def protections(self):
        return [dict(method='CooldownPeriod', stop_duration_candles=5),
                dict(method='StoplossGuard', lookback_period_candles=60,
                     trade_limit=3, stop_duration_candles=30, only_per_pair=False)]

    def bot_start(self, **kwargs):
        if self.config.get('dry_run') is not True or self.config['runmode'].value != 'dry_run':
            raise ValueError('JevBridgeStrategy only supports dry_run. No live trading or historical JEV backtest.')
        if self.config.get('trading_mode') != 'futures' or self.config.get('margin_mode') != 'isolated':
            raise ValueError('Isolated futures is required.')
        if self.config.get('max_open_trades') not in (-1, float('inf')):
            raise ValueError('Run python -m app.paper --upgrade to remove the position count cap.')
        if self.stoploss != -.50:
            raise ValueError('Run python -m app.paper --upgrade to update the leverage-aware stop fallback.')
        bridge = self.config.get('jev_bridge', {})
        url = urlparse(bridge.get('url', ''))
        if url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('The JEV bridge must be a local HTTP service.')
        if len(bridge.get('token', '')) < 32:
            raise ValueError('A bridge token of at least 32 characters is required.')
        self.bridge = bridge
        # Freqtrade may reinitialize non-trailing stops when the configured
        # fallback changes. Keep tighter existing protection across this upgrade.
        self.preserved_stops = {t.id: t.stop_loss for t in Trade.get_open_trades()
                                if number(t.stop_loss) and t.stop_loss > 0}
        self.signals = {}
        self.entries_enabled = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jev-bridge')
        self.pending = None

    def ft_bot_cleanup(self):
        if getattr(self, 'executor', None):
            self.executor.shutdown(wait=False, cancel_futures=True)
        super().ft_bot_cleanup()

    def _fetch(self):
        # Runs outside the order/stoploss loop. No AI requests are made here.
        with requests.get(self.bridge['url'].rstrip('/') + '/api/execution/signals',
                headers={'Authorization': 'Bearer ' + self.bridge['token']},
                timeout=(1, 1), allow_redirects=False) as response:
            if response.status_code != 200 or len(response.content) > 256_000:
                raise ValueError('Bridge response rejected')
            return response.json()

    def _accept(self, payload, now):
        if (payload.get('version') != 2 or payload.get('mode') != 'dry_run'
                or not number(payload.get('generated_at'))
                or not 0 <= now - payload['generated_at'] <= 15000
                or not isinstance(payload.get('signals'), list)):
            raise ValueError('Stale or invalid bridge payload')
        accepted = {}
        allowed_pairs = (self.dp.current_whitelist() if getattr(self, 'dp', None)
                         and hasattr(self.dp, 'current_whitelist') else self.config['exchange']['pair_whitelist'])
        for signal in payload['signals']:
            if not isinstance(signal, dict) or signal.get('pair') not in allowed_pairs:
                continue
            if not self._fresh(signal, now) or signal.get('action') not in ('WAIT', 'LONG', 'SHORT'):
                continue
            if signal['action'] != 'WAIT':
                if type(signal.get('leverage_requested')) is not int or signal['leverage_requested'] not in (1,2,3,5,10,15,20,25,50,75,100):
                    continue
                levels = signal.get('levels', {})
                if not isinstance(levels, dict) or not all(number(levels.get(k)) and levels[k] > 0 for k in ('entry', 'stop', 'target')):
                    continue
                stop, entry, target = (levels[k] for k in ('stop', 'entry', 'target'))
                if not (stop < entry < target if signal['action'] == 'LONG' else target < entry < stop):
                    continue
            if not isinstance(signal.get('id'), str) or len(signal['id']) != 24 or not all(c in '0123456789abcdef' for c in signal['id']):
                continue
            accepted[signal['pair']] = signal
        self.signals = accepted
        self.entries_enabled = payload.get('entries_enabled') is True

    @staticmethod
    def _fresh(signal, now):
        observed, expires = signal.get('observed_at'), signal.get('expires_at')
        return (number(observed) and number(expires) and observed <= now < expires
                and 0 < expires - observed <= 300_000)

    def bot_loop_start(self, current_time: datetime, **kwargs):
        if self.preserved_stops:
            for trade in Trade.get_open_trades():
                previous = self.preserved_stops.get(trade.id)
                if previous and trade.open_rate > 0:
                    reference = min(trade.open_rate, previous * .99) if trade.is_short else max(trade.open_rate, previous * 1.01)
                    distance = abs(reference - previous) / reference * trade.leverage
                    trade.adjust_stop_loss(reference, distance)
            Trade.commit()
        if self.pending is not None and self.pending.done():
            try:
                self._accept(self.pending.result(), current_time.timestamp() * 1000)
            except Exception as exc:
                log.warning('JEV bridge rejected (%s); entries disabled for this cycle.', type(exc).__name__)
                self.signals, self.entries_enabled = {}, False
            self.pending = None
        if self.pending is None:
            self.pending = self.executor.submit(self._fetch)
        # Freqtrade forbids unlimited count + unlimited stake. Set a numeric upper
        # bound before its leverage-tier lookup; the risk callback may reduce it.
        try:
            capital = self.wallets.get_total_stake_amount()
            available = self.wallets.get_available_stake_amount()
            if not all(number(v) and v > 0 for v in (capital, available)):
                raise ValueError('Wallet unavailable')
            self.config['stake_amount'] = min(capital * .07, available)
        except Exception:
            self.entries_enabled = False

    def _signal(self, pair, current_time):
        signal = getattr(self, 'signals', {}).get(pair)
        return signal if signal and self._fresh(signal, current_time.timestamp()*1000) else None

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe['enter_long'], dataframe['enter_short'], dataframe['enter_tag'] = 0, 0, ''
        signal = self._signal(metadata['pair'], datetime.now(timezone.utc))
        if not dataframe.empty and self.entries_enabled and signal and signal['action'] in ('LONG', 'SHORT'):
            idx = dataframe.index[-1]
            if dataframe.loc[idx, 'volume'] > 0:
                dataframe.loc[idx, 'enter_' + signal['action'].lower()] = 1
                dataframe.loc[idx, 'enter_tag'] = signal_tag(signal)
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe['exit_long'], dataframe['exit_short'] = 0, 0
        return dataframe

    def _daily_loss_hit(self, current_time):
        day = current_time.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        closed = Trade.get_trades_proxy(is_open=False, close_date=day)
        realized = sum(t.close_profit_abs or 0 for t in closed)
        # Fixed 3% of the initial paper wallet per UTC calendar day, not an equity guarantee.
        return realized <= -float(self.config['dry_run_wallet']) * .03

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force,
                            current_time, entry_tag, side, **kwargs):
        try:
            signal = self._signal(pair, current_time)
            if not self.entries_enabled or not signal or signal['action'].lower() != side or self._daily_loss_hit(current_time):
                return False
            if entry_tag != signal_tag(signal) or not number(rate) or rate <= 0:
                return False
            # Persisted history prevents re-entry on the same candle after close/restart.
            prefix = 'jev:' + signal['id'] + ':'
            if any((t.enter_tag or '').startswith(prefix) for t in Trade.get_trades_proxy(pair=pair)):
                return False
            levels = signal['levels']
            if abs(rate / levels['entry'] - 1) > .003:
                return False
            if not (levels['stop'] < rate < levels['target'] if side == 'long' else levels['target'] < rate < levels['stop']):
                return False
            book = self.dp.orderbook(pair, 1)
            bid, ask = book['bids'][0][0], book['asks'][0][0]
            if not all(number(v) for v in (bid, ask)) or not 0 < bid <= ask:
                return False
            if (ask-bid) / ((ask+bid)/2) * 10000 > 15:
                return False
            risk, reward = abs(rate-levels['stop']), abs(levels['target']-rate)
            return (reward - .0008*(rate+levels['target'])) / (risk + .0008*(rate+levels['stop'])) >= 1.5
        except Exception:
            return False

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake,
                            min_stake, max_stake, leverage, entry_tag, side, **kwargs):
        try:
            signal = self._signal(pair, current_time)
            if not self.entries_enabled or not signal or signal['action'].lower() != side or self._daily_loss_hit(current_time):
                return 0
            if entry_tag != signal_tag(signal):
                return 0
            capital = self.wallets.get_total_stake_amount()
            if not all(number(v) and v > 0 for v in (capital, current_rate, leverage, max_stake, proposed_stake)):
                return 0
            risk = abs(current_rate - signal['levels']['stop']) / current_rate
            if (risk + .0016) * leverage > .50:
                return 0
            # <=7% margin; <=0.5% capital at planned stop including estimated costs.
            stake = min(capital * .07, capital * .005 / ((risk + .0016) * leverage), max_stake, proposed_stake)
            return stake if stake >= (min_stake or 0) else 0
        except Exception:
            # Freqtrade falls back to proposed_stake if a callback raises: explicitly return zero.
            return 0

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage,
                 entry_tag, side, **kwargs):
        signal = self._signal(pair, current_time)
        if not signal or signal['action'].lower() != side or entry_tag != signal_tag(signal):
            return 1.0  # Entry callbacks reject absent/mismatched decisions.
        if not all(number(v) and v > 0 for v in (current_rate, max_leverage)):
            return 1.0
        distance = abs(current_rate - signal['levels']['stop']) / current_rate
        # Keep planned price loss + cost below half of margin. This is not an
        # exact liquidation calculation; Freqtrade additionally applies its buffer.
        risk_cap = max(1, math.floor(.50 / (distance + .0016)))
        # Risk-based leverage: the smallest leverage at which the 7% margin cap lets
        # the position carry the full 0.5% capital risk budget at its planned stop.
        # Tight stops get higher leverage, wide stops lower; the money at risk is the same.
        target = math.ceil(.005 / (.07 * (distance + .0016)) - 1e-9)
        return float(max(1, math.floor(min(target, signal['leverage_requested'], max_leverage, risk_cap, 20))))

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit,
                        after_fill=False, **kwargs):
        plan = trade_plan(trade)
        if not plan or current_rate <= 0:
            return None
        previous = self.preserved_stops.get(trade.id, plan[0])
        stop = min(plan[0], previous) if trade.is_short else max(plan[0], previous)
        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short, leverage=trade.leverage)

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        plan = trade_plan(trade)
        if not plan:
            return 'missing_risk_plan'
        stop, target = plan
        if (current_rate >= stop if trade.is_short else current_rate <= stop):
            return 'risk_stop'
        if (current_rate <= target if trade.is_short else current_rate >= target):
            return 'risk_target'
        if (current_time - trade.open_date_utc).total_seconds() >= 4 * 3600:
            return 'risk_max_hold'
        signal = self._signal(pair, current_time)
        if signal and signal.get('close_trade_id') == trade.id:
            return 'jev_close'
        return None
