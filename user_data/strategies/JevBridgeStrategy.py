"""Freqtrade 2026.8 paper executor. JEV is the only directional signal source."""
from __future__ import annotations
from typing import Any
from pandas import DataFrame
import time
from collections import Counter
from datetime import timedelta
from app.risk import finite as number, RiskPolicy, loss_fraction, net_rr, choose_leverage, size_margin, daily_loss_hit, cooldown_active
from app.bridge import VERSION, MAX_AGE_MS, MAX_PAYLOAD_BYTES, PAIR, fresh, validate_payload, signal_error
from app.metrics import event
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

log = logging.getLogger(__name__)


def signal_tag(signal: dict[str, Any]) -> str:
    levels = signal['levels']
    return f"jev:{signal['id']}:{levels['stop']:.12g}:{levels['target']:.12g}:{signal['leverage_requested']}"


def trade_plan(trade: Trade) -> tuple[float, float] | None:
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
    def protections(self) -> list[dict[str, Any]]:
        return [dict(method='CooldownPeriod', stop_duration_candles=5),
                dict(method='StoplossGuard', lookback_period_candles=60,
                     trade_limit=3, stop_duration_candles=30, only_per_pair=False)]

    def bot_start(self, **kwargs: Any) -> None:
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
        exchange = self.config.get('exchange', {})
        def contains_credentials(value: Any) -> bool:
            if not isinstance(value, dict):
                return False
            return any((k in ('key', 'apiKey', 'secret', 'password', 'uid', 'token', 'privateKey') and bool(v))
                       or contains_credentials(v) for k, v in value.items())
        if contains_credentials(exchange):
            raise ValueError('Real exchange credentials are forbidden, including in paper mode.')
        if self.config.get('force_entry_enable') is not False:
            raise ValueError('Forced entries must be disabled.')
        self.policy = RiskPolicy(**self.config.get('jev_risk', {}))
        self.bridge = bridge
        self.rejections = Counter()
        self.bridge_failures = 0
        self.next_fetch = 0.
        self.last_bridge_at = 0.
        self.bridge_lag_ms = None
        self.risk_snapshot = None
        self.marks = {}
        # Freqtrade may reinitialize non-trailing stops when the configured
        # fallback changes. Keep tighter existing protection across this upgrade.
        self.preserved_stops = {t.id: t.stop_loss for t in Trade.get_open_trades()
                                if number(t.stop_loss) and t.stop_loss > 0}
        self.signals = {}
        self.entries_enabled = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jev-bridge')
        self.pending = None

    def ft_bot_cleanup(self) -> None:
        if getattr(self, 'executor', None):
            self.executor.shutdown(wait=True, cancel_futures=True)
        if getattr(self, 'session', None):
            self.session.close()
        super().ft_bot_cleanup()

    def _fetch(self, heartbeat: dict[str, Any]) -> dict[str, Any]:
        # Persistent connection; all network I/O stays outside stop/order callbacks.
        if not hasattr(self, 'session'):
            self.session = requests.Session()
            self.session.trust_env = False
        url = self.bridge['url'].rstrip('/')
        headers = {'Authorization': 'Bearer ' + self.bridge['token']}
        response = self.session.post(url + '/api/execution/heartbeat', json=heartbeat,
                                     headers=headers, timeout=(1, 2), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Heartbeat rejected')
        with self.session.get(url + '/api/execution/signals', headers=headers,
                              timeout=(1, 2), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('Bridge response rejected')
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > MAX_PAYLOAD_BYTES:
                    raise ValueError('Bridge response oversized')
            import json
            return json.loads(data)

    def _accept(self, payload: dict[str, Any], now: float) -> None:
        validate_payload(payload, now, self.policy.identity)
        accepted = {}
        allowed_pairs = (self.dp.current_whitelist() if getattr(self, 'dp', None)
                         and hasattr(self.dp, 'current_whitelist') else self.config['exchange']['pair_whitelist'])
        for signal in payload['signals']:
            error = signal_error(signal, now)
            if error:
                self.rejections['ttl' if error == 'ttl' else 'schema'] += 1
                continue
            if signal['pair'] not in allowed_pairs:
                continue
            if signal['pair'] in accepted:
                raise ValueError('Duplicate bridge pair')
            accepted[signal['pair']] = signal
        marks = payload.get('marks', {})
        if not isinstance(marks, dict) or len(marks) > 5000:
            raise ValueError('Invalid position marks')
        for pair, mark in marks.items():
            if (not isinstance(pair, str) or not PAIR.fullmatch(pair) or not isinstance(mark, dict)
                    or not number(mark.get('price')) or mark['price'] <= 0
                    or not number(mark.get('observed_at')) or not 0 <= now-mark['observed_at'] <= MAX_AGE_MS):
                raise ValueError('Stale position mark')
        self.marks = marks
        self.signals = accepted
        self.entries_enabled = payload['entries_enabled']
        self.last_bridge_at = payload['generated_at']
        self.bridge_lag_ms = now - payload['generated_at']

    _fresh = staticmethod(fresh)

    def _restore_stops(self) -> None:
        # One-time restoration after framework initialization; normal tightening is persisted by Freqtrade.
        if not self.preserved_stops:
            return
        for trade in Trade.get_open_trades():
            previous = self.preserved_stops.get(trade.id)
            if previous and trade.open_rate > 0:
                reference = min(trade.open_rate, previous*.99) if trade.is_short else max(trade.open_rate, previous*1.01)
                trade.adjust_stop_loss(reference, abs(reference-previous)/reference*trade.leverage)
        Trade.commit()
        self.preserved_stops.clear()

    def _risk_state(self, current_time: datetime) -> dict[str, Any]:
        now = current_time.timestamp()*1000
        capital = self.wallets.get_total_stake_amount()
        if not number(capital) or capital <= 0:
            raise ValueError('Wallet unavailable')
        unrealized = 0.
        for trade in Trade.get_open_trades():
            mark = self.marks.get(trade.pair, {})
            price, timestamp = mark.get('price'), mark.get('observed_at')
            if not number(price) or price <= 0 or not number(timestamp) or not 0 <= now-timestamp <= MAX_AGE_MS:
                raise ValueError('Position mark unavailable')
            pnl = trade.calculate_profit(rate=price).profit_abs
            if not number(pnl):
                raise ValueError('Position PnL unavailable')
            unrealized += pnl
        day = current_time.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        since = min(day, current_time-timedelta(seconds=self.policy.cooldown_seconds))
        closed = Trade.get_trades_proxy(is_open=False, close_date=since)
        realized = 0.
        cooldowns = {}
        for trade in closed:
            if not number(trade.close_profit_abs) or trade.close_date_utc is None:
                raise ValueError('Closed trade telemetry unavailable')
            if trade.close_date_utc >= day:
                realized += trade.close_profit_abs
            until = trade.close_date_utc.timestamp()*1000 + self.policy.cooldown_seconds*1000
            if cooldown_active(trade.close_date_utc.timestamp()*1000, now, self.policy):
                cooldowns[trade.pair] = max(cooldowns.get(trade.pair, 0), until)
        equity = capital + unrealized
        return dict(equity=equity, unrealized=unrealized, realized_today=realized,
                    cooldowns=cooldowns, daily_loss_hit=daily_loss_hit(equity, realized, unrealized, self.policy))

    def _heartbeat(self, current_time: datetime) -> dict[str, Any]:
        try:
            self.risk_snapshot = self._risk_state(current_time)
            ready = self.risk_snapshot['equity'] > 0
        except Exception as exc:
            self.risk_snapshot = dict(equity=0., unrealized=0., realized_today=0., cooldowns={}, daily_loss_hit=True)
            self.rejections['telemetry'] += 1
            event(log, logging.WARNING, 'executor_risk', error=exc)
            ready = False
        if not ready:
            self.entries_enabled = False
        return dict(version=VERSION, mode='dry_run', generated_at=int(current_time.timestamp()*1000),
                    risk_policy=self.policy.identity, risk_ready=ready, **self.risk_snapshot,
                    rejections=dict(self.rejections), bridge_lag_ms=self.bridge_lag_ms)

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        try:
            self._loop(current_time)
        except Exception as exc:
            self.signals, self.entries_enabled = {}, False
            self.rejections['risk_error'] += 1
            event(log, logging.ERROR, 'executor_loop', error=exc)

    def _loop(self, current_time: datetime) -> None:
        try:
            self._restore_stops()
        except Exception as exc:
            self.entries_enabled = False
            event(log, logging.ERROR, 'restore_stops', error=exc)
            return
        if self.pending is not None and self.pending.done():
            try:
                self._accept(self.pending.result(), current_time.timestamp()*1000)
                self.bridge_failures = 0
            except Exception as exc:
                event(log, logging.WARNING, 'bridge', error=exc)
                self.rejections['bridge'] += 1
                self.signals, self.entries_enabled, self.marks = {}, False, {}
                self.bridge_failures += 1
            self.next_fetch = time.monotonic() + (min(30, 2**min(self.bridge_failures, 5)) if self.bridge_failures else 0)
            self.pending = None
        if not 0 <= current_time.timestamp()*1000-self.last_bridge_at <= MAX_AGE_MS:
            self.signals, self.entries_enabled = {}, False
        if self.pending is None and time.monotonic() >= self.next_fetch:
            self.pending = self.executor.submit(self._fetch, self._heartbeat(current_time))
        # Freqtrade forbids unlimited count + unlimited stake. Set a numeric upper
        # bound before its leverage-tier lookup; the risk callback may reduce it.
        try:
            capital = self.risk_snapshot['equity'] if self.risk_snapshot else 0
            available = self.wallets.get_available_stake_amount()
            if not all(number(v) and v > 0 for v in (capital, available)):
                raise ValueError('Wallet unavailable')
            self.config['stake_amount'] = min(capital * self.policy.margin_fraction, available)
        except Exception:
            self.entries_enabled = False

    def _signal(self, pair: str, current_time: datetime) -> dict[str, Any] | None:
        signal = getattr(self, 'signals', {}).get(pair)
        now = current_time.timestamp()*1000
        valid = signal and 0 <= now-self.last_bridge_at <= MAX_AGE_MS and self._fresh(signal, now)
        if signal and not valid:
            self.rejections['ttl'] += 1
        return signal if valid else None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe['enter_long'], dataframe['enter_short'], dataframe['enter_tag'] = 0, 0, ''
        signal = self._signal(metadata['pair'], datetime.now(timezone.utc))
        if not dataframe.empty and self.entries_enabled and signal and signal['action'] in ('LONG', 'SHORT'):
            idx = dataframe.index[-1]
            if dataframe.loc[idx, 'volume'] > 0:
                dataframe.loc[idx, 'enter_' + signal['action'].lower()] = 1
                dataframe.loc[idx, 'enter_tag'] = signal_tag(signal)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe['exit_long'], dataframe['exit_short'] = 0, 0
        return dataframe

    def _entry_guard(self, pair: str, current_time: datetime) -> bool:
        if not self.entries_enabled:
            self.rejections['pause'] += 1
            return False
        risk = self._risk_state(current_time)
        if risk['daily_loss_hit']:
            self.rejections['daily_loss'] += 1
            return False
        if current_time.timestamp()*1000 < risk['cooldowns'].get(pair, 0):
            self.rejections['cooldown'] += 1
            return False
        return True

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: str | None, side: str, **kwargs: Any) -> bool:
        try:
            signal = self._signal(pair, current_time)
            if not signal or signal['action'].lower() != side or not self._entry_guard(pair, current_time):
                return False
            if entry_tag != signal_tag(signal) or not number(rate) or rate <= 0:
                return False
            # Persisted history prevents re-entry on the same candle after close/restart.
            prefix = 'jev:' + signal['id'] + ':'
            if any((t.enter_tag or '').startswith(prefix) for t in Trade.get_trades_proxy(pair=pair)):
                self.rejections['duplicate'] += 1
                return False
            levels = signal['levels']
            equity = self._risk_state(current_time)['equity']
            loss = loss_fraction(rate, levels['stop'], signal['funding_cost'], self.policy)
            if not number(amount) or amount <= 0 or amount*rate*loss > equity*self.policy.capital_risk + 1e-9:
                self.rejections['margin'] += 1
                return False
            if abs(rate / levels['entry'] - 1) > self.policy.max_slippage:
                self.rejections['slippage'] += 1
                return False
            if not (levels['stop'] < rate < levels['target'] if side == 'long' else levels['target'] < rate < levels['stop']):
                return False
            book = self.dp.orderbook(pair, 1)
            bid, ask = book['bids'][0][0], book['asks'][0][0]
            if not all(number(v) for v in (bid, ask)) or not 0 < bid <= ask:
                return False
            if (ask-bid) / ((ask+bid)/2) * 10000 > self.policy.max_spread:
                self.rejections['spread'] += 1
                return False
            rr = net_rr(rate, levels['stop'], levels['target'], signal['funding_cost'], self.policy)
            allowed = rr >= self.policy.min_rr
            if allowed:
                event(log, logging.INFO, 'entry_approved', symbol=pair, signal_id=signal['id'],
                      direction=signal['action'], rate=rate, stop=levels['stop'], target=levels['target'],
                      net_rr=rr, planned_loss=amount*rate*loss, equity=equity,
                      capital_risk=self.policy.capital_risk, funding_cost=signal['funding_cost'])
            self.rejections['accepted' if allowed else 'rr'] += 1
            return allowed
        except Exception as exc:
            self.rejections['risk_error'] += 1
            event(log, logging.WARNING, 'confirm_entry', error=exc)
            return False

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs: Any) -> float:
        try:
            signal = self._signal(pair, current_time)
            if not signal or signal['action'].lower() != side or not self._entry_guard(pair, current_time):
                return 0
            if entry_tag != signal_tag(signal):
                return 0
            capital = self._risk_state(current_time)['equity']
            available = self.wallets.get_available_stake_amount()
            if not all(number(v) and v > 0 for v in (max_stake, proposed_stake, available)):
                return 0.
            loss = loss_fraction(current_rate, signal['levels']['stop'], signal['funding_cost'], self.policy)
            stake = size_margin(capital, loss, leverage, min(max_stake, proposed_stake, available), min_stake or 0., self.policy)
            if not stake:
                self.rejections['margin'] += 1
            return stake
        except Exception as exc:
            self.rejections['risk_error'] += 1
            event(log, logging.WARNING, 'size_margin', error=exc)
            return 0.  # Freqtrade otherwise falls back to proposed_stake when a callback raises.

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs: Any) -> float:
        signal = self._signal(pair, current_time)
        if not signal or signal['action'].lower() != side or entry_tag != signal_tag(signal):
            return 1.0  # Entry callbacks reject absent/mismatched decisions.
        if not all(number(v) and v > 0 for v in (current_rate, max_leverage)):
            return 1.0
        try:
            loss = loss_fraction(current_rate, signal['levels']['stop'], signal['funding_cost'], self.policy)
            return max(1., choose_leverage(loss, signal['leverage_requested'], max_leverage, self.policy))
        except (ValueError, KeyError, TypeError):
            return 1.  # Final size/confirmation reject invalid risk data.

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float,
                        after_fill: bool = False, **kwargs: Any) -> float | None:
        plan = trade_plan(trade)
        if not plan or not number(current_rate) or current_rate <= 0:
            return None
        previous = getattr(trade, 'stop_loss', None)
        if number(previous) and previous > 0 and (
                previous <= plan[0] if trade.is_short else previous >= plan[0]):
            # Keep the persisted, exchange-rounded stop. Converting it back to a ratio on
            # every tick can lose another price tick on each round-trip, especially SHORT.
            # None tells Freqtrade to retain protection, including tighter pre-upgrade stops.
            return None
        distance = stoploss_from_absolute(plan[0], current_rate, is_short=trade.is_short, leverage=trade.leverage)
        if distance > 0:
            event(log, logging.INFO, 'protective_plan', symbol=pair, trade_id=trade.id,
                  planned_stop=plan[0], target=plan[1], leverage=trade.leverage)
        return distance

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, **kwargs: Any) -> str | None:
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
