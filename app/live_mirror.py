"""Copies ONE virtual strategy (default: MACD) onto a real Binance USDT-M futures account.

Modes (.env LIVE_MODE):
    off   nothing (default)
    dry   connects with your API key, checks the account (balance, One-way mode) and writes every order it
          WOULD send to the log and the dashboard — sends nothing. Use this until the money is deposited.
    real  sends real orders.

What it does every few seconds:
  * a position the virtual strategy opens AFTER the mirror started is opened for real: same coin, side and
    leverage, isolated margin, quantity = virtual quantity × LIVE_CAPITAL / virtual starting capital
    (2000 → so with LIVE_CAPITAL=100 every order is 1/20 of the virtual one);
  * right after the fill a protective STOP_MARKET (closePosition, mark price) is placed on Binance at the
    virtual stop price, so the loss is capped even if the server is down;
  * when the virtual position closes (take-profit, signal, stop, reverse) the real one is closed with a
    reduce-only market order and its stop is cancelled;
  * if Binance's stop fired first, the position is just forgotten (never re-opened).

Safety: positions open before the mirror started are never copied; orders below Binance's minimum size are
skipped; when the futures wallet falls LIVE_MAX_LOSS_PCT (default 30) below its value at the start, no new
positions are opened (open ones still close). API key: enable Futures only, NO withdrawals, restrict to the
server IP. State: data/live-<key>.json.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger('app.live')
FAPI = 'https://fapi.binance.com'
LOOP_SECONDS = 5
MAX_LOG = 300


class LiveError(Exception):
    pass


def to_step(value: float, step: str) -> str:
    s = Decimal(step)
    v = Decimal(str(value))
    if s > 0:
        v = (v / s).to_integral_value(ROUND_DOWN) * s
    return f'{v.normalize():f}'


class LiveMirror:
    def __init__(self, engine: Any, data_dir: Path, client: httpx.AsyncClient | None = None) -> None:
        self.engine = engine
        self.mode = os.getenv('LIVE_MODE', 'off').strip().lower()
        self.key = os.getenv('LIVE_STRATEGY', 'macd').strip()
        self.api_key = os.getenv('BINANCE_API_KEY', '').strip()
        self.secret = os.getenv('BINANCE_API_SECRET', '').strip()
        self.capital = float(os.getenv('LIVE_CAPITAL', '100'))
        self.max_loss_pct = float(os.getenv('LIVE_MAX_LOSS_PCT', '30'))
        if self.mode not in ('off', 'dry', 'real'):
            self.mode = 'off'
        if self.mode != 'off' and not (self.api_key and self.secret):
            log.warning('LIVE_MODE=%s, amma BINANCE_API_KEY / BINANCE_API_SECRET yoxdur — söndürüldü', self.mode)
            self.mode = 'off'
        self.client = client or httpx.AsyncClient(timeout=20)
        self.path = data_dir / f'live-{self.key}.json'
        self.offset = 0
        self.filters: dict[str, dict] = {}
        self.filters_at = 0.0
        self.error: dict | None = None
        self.wallet: float | None = None
        self.available: float | None = None
        self.halted = False
        self.state = self._read()

    @property
    def enabled(self) -> bool:
        return self.mode in ('dry', 'real')

    # -- persistence -----------------------------------------------------------------
    def _read(self) -> dict:
        try:
            st = json.loads(self.path.read_text())
        except (OSError, ValueError):
            st = {}
        st.setdefault('started_at', None)
        st.setdefault('start_wallet', None)
        st.setdefault('positions', {})      # symbol -> {side, qty, leverage, virtual_opened_at, stop_algo_id, entry}
        st.setdefault('done', [])           # "symbol@virtual_opened_at" handled already (closed / skipped)
        st.setdefault('log', [])
        st.setdefault('realized', 0.0)
        return st

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    def _log(self, action: str, symbol: str, note: str = '', **extra: Any) -> None:
        entry = dict(ts=int(time.time() * 1000), mode=self.mode, action=action, symbol=symbol, note=note, **extra)
        self.state['log'] = (self.state['log'] + [entry])[-MAX_LOG:]
        log.warning('[%s] %s %s %s %s', self.mode.upper(), action, symbol, note, extra or '')

    # -- HTTP --------------------------------------------------------------------------
    async def _req(self, method: str, path: str, signed: bool = True, **params: Any) -> Any:
        headers = {'X-MBX-APIKEY': self.api_key}
        if signed:
            params['timestamp'] = int(time.time() * 1000) + self.offset
            params['recvWindow'] = 10000
            params['signature'] = hmac.new(self.secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        r = await self.client.request(method, FAPI + path, params=params, headers=headers)
        if r.status_code >= 400:
            raise LiveError(f'{method} {path} HTTP {r.status_code}: {r.text[:300]}')
        return r.json()

    async def _send(self, method: str, path: str, **params: Any) -> Any:
        """Order-changing call: only in real mode."""
        if self.mode != 'real':
            return None
        return await self._req(method, path, **params)

    async def _prepare(self) -> None:
        if self.filters and time.monotonic() - self.filters_at < 6 * 3600:
            return
        server = await self._req('GET', '/fapi/v1/time', signed=False)
        self.offset = int(server['serverTime']) - int(time.time() * 1000)
        if (await self._req('GET', '/fapi/v1/positionSide/dual')).get('dualSidePosition'):
            raise LiveError('Futures hesabı "Hedge Mode"-dadır — Binance-də One-way Mode-a keçirin')
        info = await self._req('GET', '/fapi/v1/exchangeInfo', signed=False)
        out = {}
        for s in info.get('symbols', []):
            f = {x['filterType']: x for x in s.get('filters', [])}
            lot = f.get('MARKET_LOT_SIZE') or f.get('LOT_SIZE') or {}
            if not lot or float(lot.get('stepSize', 0) or 0) == 0:
                lot = f.get('LOT_SIZE', {})
            out[s['symbol']] = dict(step=lot.get('stepSize', '1'), min_qty=float(lot.get('minQty', 0)),
                                    max_qty=float(lot.get('maxQty', 1e18)),
                                    tick=f.get('PRICE_FILTER', {}).get('tickSize', '0.0001'),
                                    min_notional=float(f.get('MIN_NOTIONAL', {}).get('notional', 5)),
                                    status=s.get('status'))
        self.filters = out
        self.filters_at = time.monotonic()

    # -- loop ------------------------------------------------------------------------
    async def run(self) -> None:
        if not self.enabled:
            return
        log.warning('Real hesab güzgüsü: rejim=%s, strategiya=%s, kapital=%.2f $', self.mode, self.key, self.capital)
        while True:
            try:
                await self.step()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = {'at': int(time.time() * 1000), 'message': f'{type(exc).__name__}: {exc}'[:400]}
                log.warning('Real hesab xətası: %s', self.error['message'])
            finally:
                self._save()
            await asyncio.sleep(LOOP_SECONDS)

    async def step(self) -> None:
        await self._prepare()
        st = self.state
        bal = await self._req('GET', '/fapi/v2/balance')
        usdt = next((b for b in bal if b['asset'] == 'USDT'), {})
        self.wallet = float(usdt.get('balance', 0) or 0)
        self.available = float(usdt.get('availableBalance', 0) or 0)
        if self.mode == 'dry':                       # preview as if LIVE_CAPITAL were deposited
            self.available = self.capital
        if st.get('mode') != self.mode or (self.mode == 'real' and not st['start_wallet'] and self.wallet > 0):
            # new mode (dry → real) or first deposit: start fresh; dry "positions" are never closed for real
            for sym, mine in st['positions'].items():
                self._done(f"{sym}@{mine['virtual_opened_at']}")
            st['positions'] = {}
            st['started_at'] = None
            st['mode'] = self.mode
        if st['started_at'] is None:
            st['started_at'] = int(time.time() * 1000)
            st['start_wallet'] = self.wallet
            st['realized'] = 0.0
            self._log('START', '-', f'futures balansı {self.wallet:.2f} USDT, kapital limiti {self.capital:.2f} $')
        start = st['start_wallet'] or 0
        self.halted = bool(start) and self.wallet < start * (1 - self.max_loss_pct / 100)
        acc = self.engine.ledger.accounts.get(self.key)
        if acc is None:
            raise LiveError(f'"{self.key}" adlı strategiya tapılmadı')
        virtual = {s: p for s, p in acc.positions.items()}
        real = {}
        if self.mode == 'real' and st['positions']:
            risk = await self._req('GET', '/fapi/v2/positionRisk')
            real = {r['symbol']: float(r['positionAmt']) for r in risk if float(r['positionAmt']) != 0}
        # 1) positions Binance already closed (stop fired / liquidation / closed by hand)
        if self.mode == 'real':
            for sym, mine in list(st['positions'].items()):
                if sym not in real:
                    self._forget(sym, mine, 'Binance-də artıq bağlıdır (stop işlədi və ya əl ilə bağlandı)')
        # 2) close what the virtual strategy closed (or replaced by a newer position)
        for sym, mine in list(st['positions'].items()):
            v = virtual.get(sym)
            if v is None or v.opened_at != mine['virtual_opened_at']:
                await self._close(sym, mine)
        # 3) open new virtual positions
        cap_ratio = self.capital / self.engine.settings.capital
        for sym, v in virtual.items():
            tag = f'{sym}@{v.opened_at}'
            if sym in st['positions'] or tag in st['done'] or v.opened_at < st['started_at']:
                continue
            if self.halted:
                self._done(tag)
                self._log('ATLANDI', sym, f'zərər limiti ({self.max_loss_pct:.0f}%) — yeni mövqe açılmır')
                continue
            try:
                await self._open(sym, v, v.qty * cap_ratio, tag)
            except LiveError as exc:                     # e.g. leverage not allowed — never retry this one
                self._done(tag)
                self._log('XƏTA', sym, f'açılmadı: {exc}'[:300])

    def _done(self, tag: str) -> None:
        self.state['done'] = (self.state['done'] + [tag])[-2000:]

    def _forget(self, sym: str, mine: dict, note: str) -> None:
        self.state['positions'].pop(sym, None)
        self._done(f"{sym}@{mine['virtual_opened_at']}")
        self._log('BAĞLANIB', sym, note)

    async def _open(self, sym: str, v: Any, qty: float, tag: str) -> None:
        f = self.filters.get(sym)
        side = 'BUY' if v.side > 0 else 'SELL'
        price = v.fill_price
        if not f or f['status'] != 'TRADING':
            self._done(tag)
            self._log('ATLANDI', sym, 'Binance-də ticarət olunmur')
            return
        q = to_step(min(qty, f['max_qty']), f['step'])
        notional = float(q) * price
        if float(q) < f['min_qty'] or notional < f['min_notional']:
            self._done(tag)
            self._log('ATLANDI', sym, f'order çox kiçikdir: {qty * price:.2f} $ < Binance minimumu {max(f["min_notional"], f["min_qty"] * price):.2f} $')
            return
        margin = notional / v.leverage
        if self.available is not None and margin > self.available * 0.95:
            self._done(tag)
            self._log('ATLANDI', sym, f'balans çatmır: marja {margin:.2f} $, mövcud {self.available:.2f} $')
            return
        stop = to_step(v.stop_price, f['tick']) if v.stop_price else None
        if self.mode == 'real':
            try:
                await self._req('POST', '/fapi/v1/marginType', symbol=sym, marginType='ISOLATED')
            except LiveError as exc:
                if '-4046' not in str(exc):          # "No need to change margin type"
                    raise
            await self._req('POST', '/fapi/v1/leverage', symbol=sym, leverage=v.leverage)
            res = await self._req('POST', '/fapi/v1/order', symbol=sym, side=side, type='MARKET', quantity=q,
                                  newOrderRespType='RESULT')
            q = res.get('executedQty', q)
            price = float(res.get('avgPrice') or price)
            if self.available is not None:
                self.available -= margin
        mine = dict(side=v.side, qty=q, leverage=v.leverage, virtual_opened_at=v.opened_at, entry=price, stop=stop, stop_algo_id=None)
        self.state['positions'][sym] = mine
        self._log('AÇ', sym, f"{'LONG' if v.side > 0 else 'SHORT'} {v.leverage}x", qty=q, price=price,
                  value=round(float(q) * price, 2), stop=stop)
        if stop:
            try:
                res = await self._send('POST', '/fapi/v1/algoOrder', algoType='CONDITIONAL', symbol=sym,
                                       side='SELL' if v.side > 0 else 'BUY', type='STOP_MARKET', triggerPrice=stop,
                                       closePosition='true', workingType='MARK_PRICE')
                mine['stop_algo_id'] = (res or {}).get('algoId')
                self._log('STOP', sym, f'qoruyucu stop {stop}')
            except LiveError as exc:
                self._log('XƏTA', sym, f'stop qoyulmadı: {exc}'[:300])

    async def _close(self, sym: str, mine: dict) -> None:
        if mine.get('stop_algo_id'):
            try:
                await self._send('DELETE', '/fapi/v1/algoOrder', algoId=mine['stop_algo_id'])
            except LiveError:
                pass                                   # already triggered / gone
        price = self.engine.prices.get(sym, mine['entry'])
        if self.mode == 'real':
            res = await self._req('POST', '/fapi/v1/order', symbol=sym, side='SELL' if mine['side'] > 0 else 'BUY',
                                  type='MARKET', quantity=mine['qty'], reduceOnly='true', newOrderRespType='RESULT')
            price = float(res.get('avgPrice') or price)
        pnl = mine['side'] * float(mine['qty']) * (price - mine['entry'])
        self.state['realized'] += pnl
        self.state['positions'].pop(sym, None)
        self._done(f"{sym}@{mine['virtual_opened_at']}")
        self._log('BAĞLA', sym, 'virtual strategiya bağladı', qty=mine['qty'], price=price, pnl=round(pnl, 4))

    # -- API -------------------------------------------------------------------------
    def summary(self) -> dict:
        st = self.state
        pos = [dict(symbol=s, side='LONG' if p['side'] > 0 else 'SHORT', qty=p['qty'], leverage=p['leverage'], entry=p['entry'],
                    stop=p.get('stop'), price=self.engine.prices.get(s),
                    pnl=p['side'] * float(p['qty']) * (self.engine.prices.get(s, p['entry']) - p['entry']))
               for s, p in st['positions'].items()]
        return dict(mode=self.mode, strategy=self.key, capital=self.capital, wallet=self.wallet, available=self.available,
                    start_wallet=st['start_wallet'], started_at=st['started_at'], halted=self.halted,
                    max_loss_pct=self.max_loss_pct, realized=st['realized'], positions=pos,
                    log=list(reversed(st['log'][-80:])), error=self.error)
