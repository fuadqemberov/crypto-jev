"""Virtual spot accounts: one independent USDT wallet per strategy.

Accounting (long-only spot, no leverage), fee ``f`` and slippage ``s`` per side:

* buy  ``N`` USDT at price ``p``: fee = N·f, fill = p·(1+s), qty = (N − fee) / fill, cash −= N
* sell ``qty`` at price ``p``:    fill = p·(1−s), gross = qty·fill, proceeds = gross·(1−f), cash += proceeds
* open positions are marked at their liquidation value (what a sell would return right now),
  so equity already includes the exit costs and never flatters an open trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .store import Store


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float      # market price at the decision
    fill_price: float       # price after slippage
    cost: float             # USDT spent including the entry fee
    fee: float
    opened_at: int


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
                self.accounts[p['key']].positions[p['symbol']] = Position(
                    p['symbol'], p['qty'], p['entry_price'], p['fill_price'], p['cost'], p['fee'], p['opened_at'])
                self.last_price.setdefault(p['symbol'], p['entry_price'])

    # -- valuation ------------------------------------------------------------------
    def price(self, symbol: str) -> float:
        return self.last_price[symbol]

    def liquidation_value(self, pos: Position, price: float | None = None) -> float:
        price = self.last_price.get(pos.symbol, pos.entry_price) if price is None else price
        return pos.qty * price * (1 - self.settings.slippage_rate) * (1 - self.settings.fee_rate)

    def equity(self, key: str) -> float:
        acc = self.accounts[key]
        return acc.cash + sum(self.liquidation_value(p) for p in acc.positions.values())

    def update_prices(self, prices: dict[str, float]) -> dict[str, float]:
        """Store fresh prices, update running peak / drawdown, return equity per account."""
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
    def order_size(self, key: str) -> float:
        acc = self.accounts[key]
        if len(acc.positions) >= self.settings.max_positions:
            return 0.0
        size = min(self.equity(key) / self.settings.max_positions, acc.cash)
        return size if size >= self.settings.min_order else 0.0

    def buy(self, key: str, symbol: str, price: float, ts: int) -> Position | None:
        acc = self.accounts[key]
        notional = self.order_size(key)
        if symbol in acc.positions or notional <= 0 or not price > 0:
            return None
        fee = notional * self.settings.fee_rate
        fill = price * (1 + self.settings.slippage_rate)
        pos = Position(symbol, (notional - fee) / fill, price, fill, notional, fee, ts)
        cash = acc.cash - notional
        self.store.open_position(key, cash, pos.__dict__)
        acc.cash = cash
        acc.positions[symbol] = pos
        self.last_price[symbol] = price
        return pos

    def sell(self, key: str, symbol: str, price: float, ts: int, reason: str) -> dict | None:
        acc = self.accounts[key]
        pos = acc.positions.get(symbol)
        if pos is None or not price > 0:
            return None
        fill = price * (1 - self.settings.slippage_rate)
        gross = pos.qty * fill
        exit_fee = gross * self.settings.fee_rate
        proceeds = gross - exit_fee
        trade = dict(symbol=symbol, qty=pos.qty, entry_price=pos.entry_price, exit_price=price, cost=pos.cost,
                     proceeds=proceeds, pnl=proceeds - pos.cost, ret=proceeds / pos.cost - 1,
                     fees=pos.fee + exit_fee, opened_at=pos.opened_at, closed_at=ts, reason=reason)
        cash = acc.cash + proceeds
        trade['id'] = self.store.close_position(key, cash, trade)
        acc.cash = cash
        del acc.positions[symbol]
        self.last_price[symbol] = price
        return trade
