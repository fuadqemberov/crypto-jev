"""Virtual USDT-M futures accounts: one independent wallet per strategy, isolated margin.

For a position with side ``d`` (+1 long, −1 short), margin ``M``, leverage ``L``, taker fee ``f``
and slippage ``s`` per side, opened at price ``p``:

* open:  notional = M·L, fill = p·(1 + d·s), qty = notional / fill, entry fee = notional·f,
         wallet −= M + entry fee
* mark:  unrealized = d·qty·(mark − fill)
* close: exit fill = mark·(1 − d·s), gross = d·qty·(exit − fill), exit fee = qty·exit·f,
         proceeds = max(0, M + gross − exit fee), wallet += proceeds
* funding at each settlement: payment = d·qty·mark·rate is taken from (or added to) the margin
* liquidation: when M + unrealized ≤ maint_rate·qty·mark the whole margin is lost
* take profit: closed as soon as the net PnL (after both fees and funding) ≥ TAKE_PROFIT_USDT
* stop loss: a stop price fixed at the open, ``fill − d·distance``. The distance is the strategy's
  ATR multiple × ATR(14), but never more than STOP_LOSS_PCT of the margin (STOP_LOSS_PCT/100 · fill / L),
  so the stop always sits well before the liquidation price (≈ fill · (1/L − maint)).

Open positions are valued at what closing them right now would return, so account equity
already includes exit costs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .config import Settings
from .store import Store


@dataclass
class Position:
    symbol: str
    side: int               # +1 long / -1 short
    leverage: int
    qty: float
    entry_price: float      # mark price at the decision
    fill_price: float       # after slippage
    margin: float           # current isolated margin (after funding)
    fee: float              # entry fee
    funding: float          # net funding paid (+) / received (−)
    opened_at: int
    stop_price: float = 0.0   # 0 = no stop

    @property
    def initial_margin(self) -> float:
        return self.margin + self.funding

    @property
    def notional(self) -> float:
        return self.qty * self.fill_price


@dataclass
class Account:
    key: str
    capital: float
    cash: float
    started_at: int
    positions: dict[str, Position] = field(default_factory=dict)
    peak: float = 0.0
    max_dd: float = 0.0


class Ledger:
    def __init__(self, settings: Settings, store: Store, keys: list[str], now_ms: int) -> None:
        self.settings = settings
        self.store = store
        self.last_price: dict[str, float] = {}
        rows = store.accounts()
        for key in keys:
            if key not in rows:
                store.create_account(key, settings.capital, now_ms)
        rows = store.accounts()
        peaks = store.equity_peaks()
        self.accounts: dict[str, Account] = {}
        for key in keys:
            r = rows[key]
            peak, dd = peaks.get(key, (r['capital'], 0.0))
            self.accounts[key] = Account(key, r['capital'], r['cash'], r['started_at'], peak=max(peak, r['capital']), max_dd=dd)
        for p in store.positions():
            if p['key'] in self.accounts:
                pos = Position(**{k: p[k] for k in Position.__dataclass_fields__})
                if not pos.stop_price:                     # opened before the stop-loss existed
                    pos.stop_price = self.stop_price(pos.side, pos.fill_price, pos.leverage)
                self.accounts[p['key']].positions[p['symbol']] = pos
                self.last_price.setdefault(p['symbol'], p['entry_price'])

    # -- valuation ------------------------------------------------------------------
    def mark(self, pos: Position, price: float | None = None) -> float:
        return self.last_price.get(pos.symbol, pos.entry_price) if price is None else price

    def unrealized(self, pos: Position, price: float | None = None) -> float:
        return pos.side * pos.qty * (self.mark(pos, price) - pos.fill_price)

    def close_value(self, pos: Position, price: float | None = None) -> float:
        price = self.mark(pos, price)
        exit_fill = price * (1 - pos.side * self.settings.slippage_rate)
        gross = pos.side * pos.qty * (exit_fill - pos.fill_price)
        return max(0.0, pos.margin + gross - pos.qty * exit_fill * self.settings.fee_rate)

    def net_pnl(self, pos: Position, price: float | None = None) -> float:
        """What closing now would add to the account versus never opening (fees + funding included)."""
        return self.close_value(pos, price) - pos.initial_margin - pos.fee

    def liquidation_price(self, pos: Position) -> float:
        q, e, m, mmr = pos.qty, pos.fill_price, pos.margin, self.settings.maint_margin_rate
        if pos.side > 0:
            return max(0.0, (q * e - m) / (q * (1 - mmr)))
        return (m + q * e) / (q * (1 + mmr))

    def stop_price(self, side: int, fill: float, leverage: int, distance: float | None = None) -> float:
        """Stop at ``distance`` (price units) from the fill, capped at STOP_LOSS_PCT of the margin."""
        cap_pct = self.settings.stop_loss_pct
        if cap_pct <= 0 or not fill > 0:
            return 0.0
        cap = cap_pct / 100 * fill / max(1, leverage)
        d = cap if distance is None or not distance > 0 else min(distance, cap)
        return max(0.0, fill - side * d)

    def is_stopped(self, pos: Position, price: float) -> bool:
        return pos.stop_price > 0 and (price <= pos.stop_price if pos.side > 0 else price >= pos.stop_price)

    def is_liquidated(self, pos: Position, price: float) -> bool:
        return pos.margin + self.unrealized(pos, price) <= self.settings.maint_margin_rate * pos.qty * price

    def equity(self, key: str) -> float:
        acc = self.accounts[key]
        return acc.cash + sum(self.close_value(p) for p in acc.positions.values())

    def update_prices(self, prices: dict[str, float]) -> dict[str, float]:
        """Store fresh marks, update running peak / drawdown, return equity per account."""
        self.last_price.update({k: v for k, v in prices.items() if v > 0})
        out = {}
        for key, acc in self.accounts.items():
            eq = self.equity(key)
            acc.peak = max(acc.peak, eq)
            if acc.peak > 0:
                acc.max_dd = max(acc.max_dd, 1 - eq / acc.peak)
            out[key] = eq
        return out

    def held_symbols(self) -> set[str]:
        return {s for acc in self.accounts.values() for s in acc.positions}

    # -- orders ---------------------------------------------------------------------
    def order_margin(self, key: str, leverage: int) -> float:
        acc = self.accounts[key]
        target = self.equity(key) * self.settings.margin_pct / 100
        affordable = acc.cash / (1 + leverage * self.settings.fee_rate)   # margin + entry fee must fit
        margin = min(target, affordable)
        return margin if margin >= self.settings.min_margin else 0.0

    def open(self, key: str, symbol: str, side: int, leverage: int, price: float, ts: int,
             stop_distance: float | None = None) -> Position | None:
        acc = self.accounts[key]
        if symbol in acc.positions or side not in (1, -1) or not price > 0 or leverage < 1:
            return None
        margin = self.order_margin(key, leverage)
        if margin <= 0:
            return None
        notional = margin * leverage
        fill = price * (1 + side * self.settings.slippage_rate)
        fee = notional * self.settings.fee_rate
        pos = Position(symbol, side, leverage, notional / fill, price, fill, margin, fee, 0.0, ts,
                       self.stop_price(side, fill, leverage, stop_distance))
        cash = acc.cash - margin - fee
        self.store.open_position(key, cash, asdict(pos))
        acc.cash = cash
        acc.positions[symbol] = pos
        self.last_price[symbol] = price
        return pos

    def close(self, key: str, symbol: str, price: float, ts: int, reason: str) -> dict | None:
        acc = self.accounts[key]
        pos = acc.positions.get(symbol)
        if pos is None or not price > 0:
            return None
        exit_fill = price * (1 - pos.side * self.settings.slippage_rate)
        if reason == 'liquidation':
            proceeds, exit_fee = 0.0, 0.0
        else:
            exit_fee = pos.qty * exit_fill * self.settings.fee_rate
            proceeds = self.close_value(pos, price)
        cost = pos.initial_margin + pos.fee
        trade = dict(symbol=symbol, side=pos.side, leverage=pos.leverage, qty=pos.qty, entry_price=pos.entry_price,
                     exit_price=price, margin=pos.initial_margin, notional=pos.notional, proceeds=proceeds,
                     pnl=proceeds - cost, ret=(proceeds - cost) / pos.initial_margin, fees=pos.fee + exit_fee,
                     funding=pos.funding, opened_at=pos.opened_at, closed_at=ts, reason=reason)
        cash = acc.cash + proceeds
        trade['id'] = self.store.close_position(key, cash, trade)
        acc.cash = cash
        del acc.positions[symbol]
        self.last_price[symbol] = price
        return trade

    def apply_funding(self, symbol: str, rate: float, price: float, funding_time: int) -> float:
        """Settle one funding period for every position in ``symbol`` opened before it. Returns total paid."""
        rows, total = [], 0.0
        for acc in self.accounts.values():
            pos = acc.positions.get(symbol)
            if pos is None or pos.opened_at >= funding_time:
                continue
            payment = pos.side * pos.qty * price * rate
            pos.margin -= payment
            pos.funding += payment
            total += payment
            rows.append((pos.margin, pos.funding, acc.key, symbol))
        if rows:
            self.store.update_margin(rows)
        return total
