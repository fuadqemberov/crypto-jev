"""REAL orders on Binance: mirrors the mixed portfolio (app/mix.py) onto your own account.

OFF unless LIVE_TRADING=true and BINANCE_API_KEY / BINANCE_API_SECRET are set in .env.

How it works — every minute, after the virtual portfolio decides, the executor compares what the bot
should hold with what IT has bought (it tracks its own quantities, so it never sells coins you hold
yourself) and sends market orders for the difference:

    DCA + trend   spot holdings (BTC, ETH and the trend coins) — no leverage, no liquidation
    carry         spot holding + USDT-M futures SHORT of the same quantity (leverage 2, one-way mode)

Order of each round: sells / short closes first, then USDT is moved between the spot and futures wallets
(futures keeps 70 % of the short notional as margin), then buys, then new shorts (only once the spot leg
is bought). A difference is traded only when it is above 10 USDT and 5 % of the target.

Limits (.env): LIVE_CAPITAL (default 2000) is the most the bot may put to work; one order is at most 25 %
of it; LIVE_MAX_LOSS_PCT (default 30): when the managed value falls that far below LIVE_CAPITAL the bot
stops opening anything new (it still closes) until you raise the limit or reset. State: data/live.json.

API key: enable "Spot & Margin trading", "Futures" and "Permits Universal Transfer"; NEVER enable
withdrawals; restrict it to your server's IP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger('app.live')
SPOT = 'https://api.binance.com'
FAPI = 'https://fapi.binance.com'
SHORT_LEVERAGE = 2
MARGIN_SHARE = 0.70
MIN_TRADE_USDT = 10.0
TOLERANCE = 0.05
MAX_ORDER_SHARE = 0.25


class LiveError(Exception):
    pass


def floor_step(qty: float, step: str) -> float:
    s = Decimal(step)
    if s <= 0:
        return qty
    return float((Decimal(str(qty)) / s).to_integral_value(ROUND_DOWN) * s)


class LiveExecutor:
    def __init__(self, data_dir: Path, client: httpx.AsyncClient | None = None) -> None:
        self.key = os.getenv('BINANCE_API_KEY', '').strip()
        self.secret = os.getenv('BINANCE_API_SECRET', '').strip()
        self.enabled = os.getenv('LIVE_TRADING', 'false').strip().lower() == 'true' and bool(self.key and self.secret)
        self.capital = float(os.getenv('LIVE_CAPITAL', '2000'))
        self.max_loss_pct = float(os.getenv('LIVE_MAX_LOSS_PCT', '30'))
        self.client = client or httpx.AsyncClient(timeout=20)
        self.path = data_dir / 'live.json'
        self.offset = 0
        self.spot_filters: dict[str, dict] = {}
        self.fut_filters: dict[str, dict] = {}
        self.filters_at = 0.0
        self.prices: dict[str, float] = {}
        self.error: dict | None = None
        self.halted = False
        self.state = self._read()

    # -- persistence -----------------------------------------------------------------
    def _read(self) -> dict:
        try:
            st = json.loads(self.path.read_text())
        except (OSError, ValueError):
            st = {}
        st.setdefault('spot', {})            # symbol -> qty the bot owns
        st.setdefault('short', {})           # symbol -> futures short qty the bot opened
        st.setdefault('orders', [])
        st.setdefault('fees', 0.0)
        st.setdefault('leverage_set', [])
        return st

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    # -- HTTP --------------------------------------------------------------------------
    async def _req(self, method: str, base: str, path: str, signed: bool = False, **params: Any) -> Any:
        headers = {'X-MBX-APIKEY': self.key} if self.key else {}
        if signed:
            params['timestamp'] = int(time.time() * 1000) + self.offset
            params['recvWindow'] = 10000
            query = urlencode(params)
            params['signature'] = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        r = await self.client.request(method, base + path, params=params, headers=headers)
        if r.status_code >= 400:
            raise LiveError(f'{method} {path} HTTP {r.status_code}: {r.text[:300]}')
        return r.json()

    async def _prepare(self) -> None:
        if time.monotonic() - self.filters_at < 6 * 3600 and self.spot_filters:
            return
        server = await self._req('GET', SPOT, '/api/v3/time')
        self.offset = int(server['serverTime']) - int(time.time() * 1000)
        if (await self._req('GET', FAPI, '/fapi/v1/positionSide/dual', signed=True)).get('dualSidePosition'):
            raise LiveError('Futures hesabı "Hedge Mode"-dadır — Binance-də "One-way Mode"-a keçirin.')

        def parse(info: dict) -> dict[str, dict]:
            out = {}
            for s in info.get('symbols', []):
                f = {x['filterType']: x for x in s.get('filters', [])}
                lot = f.get('MARKET_LOT_SIZE') or f.get('LOT_SIZE') or {}
                if not lot or float(lot.get('stepSize', 0)) == 0:
                    lot = f.get('LOT_SIZE', {})
                notional = f.get('NOTIONAL') or f.get('MIN_NOTIONAL') or {}
                out[s['symbol']] = dict(step=lot.get('stepSize', '0'), min_qty=float(lot.get('minQty', 0)),
                                        min_notional=float(notional.get('minNotional', notional.get('notional', 5)) or 5),
                                        status=s.get('status'))
            return out
        self.spot_filters = parse(await self._req('GET', SPOT, '/api/v3/exchangeInfo', permissions='SPOT'))
        self.fut_filters = parse(await self._req('GET', FAPI, '/fapi/v1/exchangeInfo'))
        self.filters_at = time.monotonic()

    def _record(self, kind: str, symbol: str, side: str, qty: float, price: float, note: str = '') -> None:
        self.state['orders'] = (self.state['orders'] + [dict(ts=int(time.time() * 1000), kind=kind, symbol=symbol, side=side,
                                                             qty=qty, price=price, value=round(qty * price, 2), note=note)])[-300:]
        log.warning('REAL ORDER %s %s %s %s @ %s %s', kind, side, symbol, qty, price, note)

    # -- orders ------------------------------------------------------------------------
    async def _spot(self, symbol: str, side: str, qty: float) -> float:
        f = self.spot_filters[symbol]
        qty = floor_step(qty, f['step'])
        price = self.prices[symbol]
        if qty <= 0 or qty < f['min_qty'] or qty * price < max(f['min_notional'], MIN_TRADE_USDT):
            return 0.0
        qty = min(qty, floor_step(self.capital * MAX_ORDER_SHARE / price, f['step']))
        res = await self._req('POST', SPOT, '/api/v3/order', signed=True, symbol=symbol, side=side, type='MARKET',
                              quantity=f'{Decimal(str(qty)).normalize():f}', newOrderRespType='FULL')
        filled = float(res.get('executedQty', 0))
        quote = float(res.get('cummulativeQuoteQty', 0))
        base = symbol[:-4]
        fee_base = sum(float(x['commission']) for x in res.get('fills', []) if x.get('commissionAsset') == base)
        fee_usdt = sum(float(x['commission']) * float(x['price']) for x in res.get('fills', []))
        self.state['fees'] += fee_usdt
        avg = quote / filled if filled else price
        net = filled - fee_base if side == 'BUY' else filled
        self._record('spot', symbol, side, filled, avg)
        return net if side == 'BUY' else -filled

    async def _future(self, symbol: str, side: str, qty: float, reduce: bool) -> float:
        f = self.fut_filters[symbol]
        qty = floor_step(qty, f['step'])
        price = self.prices[symbol]
        if qty <= 0 or qty < f['min_qty'] or (not reduce and qty * price < max(f['min_notional'], MIN_TRADE_USDT)):
            return 0.0
        if not reduce and symbol not in self.state['leverage_set']:
            await self._req('POST', FAPI, '/fapi/v1/leverage', signed=True, symbol=symbol, leverage=SHORT_LEVERAGE)
            self.state['leverage_set'].append(symbol)
        params = dict(symbol=symbol, side=side, type='MARKET', quantity=f'{Decimal(str(qty)).normalize():f}', newOrderRespType='RESULT')
        if reduce:
            params['reduceOnly'] = 'true'
        res = await self._req('POST', FAPI, '/fapi/v1/order', signed=True, **params)
        filled = float(res.get('executedQty', 0))
        avg = float(res.get('avgPrice') or price)
        self.state['fees'] += filled * avg * 0.0005
        self._record('futures', symbol, side, filled, avg, 'short bağla' if reduce else 'short aç')
        return filled

    async def _transfer(self, to_futures: bool, amount: float) -> None:
        amount = math.floor(amount * 100) / 100
        if amount < 1:
            return
        await self._req('POST', SPOT, '/sapi/v1/asset/transfer', signed=True,
                        type='MAIN_UMFUTURE' if to_futures else 'UMFUTURE_MAIN', asset='USDT', amount=f'{amount:.2f}')
        self._record('transfer', 'USDT', 'spot→futures' if to_futures else 'futures→spot', amount, 1.0)

    # -- one round ---------------------------------------------------------------------
    async def reconcile(self, spot_target: dict[str, float], short_target: dict[str, float], virtual_total: float) -> None:
        if not self.enabled:
            return
        try:
            await self._prepare()
            tick = await self._req('GET', SPOT, '/api/v3/ticker/price')
            self.prices = {t['symbol']: float(t['price']) for t in tick}
            await self._round(spot_target, short_target, virtual_total)
            self.error = None
        except Exception as exc:
            self.error = {'at': int(time.time() * 1000), 'message': f'{type(exc).__name__}: {exc}'[:400]}
            log.warning('Real icra xətası: %s', self.error['message'])
        finally:
            self._save()

    def _diff_ok(self, diff: float, target: float, price: float) -> bool:
        return abs(diff) * price >= MIN_TRADE_USDT and abs(diff) >= TOLERANCE * max(abs(target), 1e-12)

    async def _round(self, spot_target: dict[str, float], short_target: dict[str, float], virtual_total: float) -> None:
        st = self.state
        # only coins that trade on both books with the same symbol (futures symbols like 1000PEPEUSDT are skipped)
        spot_target = {s: q for s, q in spot_target.items() if self.spot_filters.get(s, {}).get('status') == 'TRADING' and s in self.prices}
        short_target = {s: q for s, q in short_target.items() if s in spot_target and s in self.fut_filters}
        # loss limit: virtual portfolio value relative to the capital it started with
        self.halted = virtual_total < self.capital * (1 - self.max_loss_pct / 100)
        # 1) close shorts that are too big
        for s, have in list(st['short'].items()):
            want = short_target.get(s, 0.0)
            if have - want > 0 and (want == 0 or self._diff_ok(have - want, want, self.prices.get(s, 0))):
                done = await self._future(s, 'BUY', have - want if want else have, reduce=True)
                st['short'][s] = max(have - done, 0.0)
                if st['short'][s] <= self.fut_filters.get(s, {}).get('min_qty', 0):
                    st['short'].pop(s)
        # 2) spot sells (never more than the bot itself bought)
        for s, have in list(st['spot'].items()):
            want = spot_target.get(s, 0.0)
            if have - want > 0 and s in self.prices and (want == 0 or self._diff_ok(have - want, want, self.prices[s])):
                base = s[:-4]
                acct = await self._req('GET', SPOT, '/api/v3/account', signed=True, omitZeroBalances='true')
                free = next((float(b['free']) for b in acct['balances'] if b['asset'] == base), 0.0)
                sold = -await self._spot(s, 'SELL', min(have - want, free))
                st['spot'][s] = max(have - sold, 0.0)
                if st['spot'][s] * self.prices[s] < 1:
                    st['spot'].pop(s)
        # 3) futures margin: 70 % of the short notional, the rest back to spot
        need = sum(q * self.prices[s] for s, q in short_target.items()) * MARGIN_SHARE
        bal = await self._req('GET', FAPI, '/fapi/v2/balance', signed=True)
        fut_usdt = next((float(b['availableBalance']) for b in bal if b['asset'] == 'USDT'), 0.0)
        used = sum(q * self.prices.get(s, 0) for s, q in st['short'].items()) / SHORT_LEVERAGE
        if need - used - fut_usdt > MIN_TRADE_USDT and not self.halted:
            acct = await self._req('GET', SPOT, '/api/v3/account', signed=True, omitZeroBalances='true')
            spot_usdt = next((float(b['free']) for b in acct['balances'] if b['asset'] == 'USDT'), 0.0)
            await self._transfer(True, min(need - used - fut_usdt, spot_usdt * 0.5))
        elif fut_usdt - max(need - used, 0) > 50:
            await self._transfer(False, fut_usdt - max(need - used, 0) - 20)
        if self.halted:
            log.warning('Zərər limiti (%s%%) — yeni alış / short açılmır', self.max_loss_pct)
            return
        # 4) spot buys, capped by free USDT and the capital limit
        acct = await self._req('GET', SPOT, '/api/v3/account', signed=True, omitZeroBalances='true')
        usdt = next((float(b['free']) for b in acct['balances'] if b['asset'] == 'USDT'), 0.0)
        invested = sum(q * self.prices.get(s, 0) for s, q in st['spot'].items())
        for s, want in sorted(spot_target.items(), key=lambda x: -x[1] * self.prices[x[0]]):
            have = st['spot'].get(s, 0.0)
            diff = want - have
            if diff <= 0 or not self._diff_ok(diff, want, self.prices[s]):
                continue
            room = min(usdt * 0.995, self.capital - invested)
            qty = min(diff, room / self.prices[s] / 1.002)
            if qty * self.prices[s] < MIN_TRADE_USDT:
                continue
            got = await self._spot(s, 'BUY', qty)
            if got:
                st['spot'][s] = have + got
                usdt -= got * self.prices[s] * 1.001
                invested += got * self.prices[s]
        # 5) shorts, never larger than the spot leg the bot holds
        for s, want in short_target.items():
            have = st['short'].get(s, 0.0)
            cap = st['spot'].get(s, 0.0)
            diff = min(want, cap) - have
            if diff > 0 and self._diff_ok(diff, want, self.prices[s]):
                got = await self._future(s, 'SELL', diff, reduce=False)
                if got:
                    st['short'][s] = have + got

    def summary(self) -> dict:
        st = self.state
        spot_value = sum(q * self.prices.get(s, 0) for s, q in st['spot'].items())
        return dict(enabled=self.enabled, capital=self.capital, halted=self.halted, max_loss_pct=self.max_loss_pct,
                    spot=[dict(symbol=s, qty=q, value=q * self.prices.get(s, 0)) for s, q in sorted(st['spot'].items())],
                    shorts=[dict(symbol=s, qty=q, value=q * self.prices.get(s, 0)) for s, q in sorted(st['short'].items())],
                    spot_value=spot_value, fees=st['fees'], orders=list(reversed(st['orders'][-50:])), error=self.error)
