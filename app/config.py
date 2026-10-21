"""Runtime settings for the top-5 consensus USDT-M futures paper-trading system (read from .env)."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

TIMEFRAMES = {'15m': 900_000, '30m': 1_800_000, '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000}


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...] = ('ALL',)
    top_n: int = 50
    timeframe: str = '1h'
    capital: float = 2000.0
    margin_pct: float = 5.0              # margin per order, % of the account's current equity
    min_margin: float = 5.0              # smallest margin worth opening (USDT)
    take_profit: float = 0.0             # optional fixed TP: close when net PnL >= this (USDT); 0 = off
    tp_r_multiple: float = 1.5           # take-profit at this multiple of the stop distance (R); 0 = off
    risk_per_move: float = 0.25          # leverage = floor(this / (ATR% x strategy ATR multiple))
    stop_loss_pct: float = 50.0          # stop-loss: at most this % of the margin may be lost; 0 = off
    max_leverage: int = 100              # global ceiling for the leverage a strategy may choose
    max_cost_r: float = 0.10             # skip an entry if round-trip fee+slippage > this share of the stop distance; 0 = off
    signal_exits: str = 'all'            # all: every rule exits on its opposite signal; auto: only own-exit rules; none
    cooldown_bars: int = 0               # after a close, the same strategy waits this many candles on that coin; 0 = off
    max_hold_hours: float = 0.0          # close a position held longer than this; 0 = off
    fee_rate: float = 0.0005             # taker fee per side (Binance USDT-M default 0.05%)
    slippage_rate: float = 0.0005
    maint_margin_rate: float = 0.005     # maintenance margin used for the liquidation check
    history_bars: int = 500
    price_seconds: int = 15
    candle_delay_seconds: int = 5
    kline_parallelism: int = 6
    data_dir: Path = Path('data')
    binance_url: str = 'https://fapi.binance.com/fapi/v1'
    user: str = ''
    password: str = field(default='', repr=False)
    demo: bool = False
    demo_candle_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.symbols != ('ALL',) and (not self.symbols or any(not re.fullmatch(r'[A-Z0-9]{2,20}USDT', s) for s in self.symbols)):
            raise ValueError('SYMBOLS: ALL və ya vergüllə ayrılmış USDT cütləri (məs. BTCUSDT,ETHUSDT) olmalıdır.')
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f'TIMEFRAME bunlardan biri olmalıdır: {", ".join(TIMEFRAMES)}')
        for name, low, high in [('top_n', 1, 500), ('max_leverage', 1, 125), ('history_bars', 250, 1000),
                                ('price_seconds', 5, 300), ('candle_delay_seconds', 0, 120), ('kline_parallelism', 1, 16),
                                ('cooldown_bars', 0, 500)]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name.upper()} {low}–{high} aralığında tam ədəd olmalıdır.')
        for name, low, high in [('capital', 10, 10_000_000), ('margin_pct', 0.1, 100), ('min_margin', 0.1, 10_000),
                                ('take_profit', 0, 1_000_000), ('stop_loss_pct', 0, 90), ('tp_r_multiple', 0, 20), ('risk_per_move', 0.01, 1), ('max_cost_r', 0, 1), ('max_hold_hours', 0, 10_000), ('fee_rate', 0, 0.01), ('slippage_rate', 0, 0.01),
                                ('maint_margin_rate', 0, 0.1), ('demo_candle_seconds', 1, 3600)]:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name.upper()} {low}–{high} aralığında olmalıdır.')
        if self.capital * self.margin_pct / 100 < self.min_margin:
            raise ValueError('START_CAPITAL × ORDER_MARGIN_PCT minimum marjadan (MIN_MARGIN_USDT) kiçikdir.')
        if self.maint_margin_rate >= 1 / self.max_leverage:
            raise ValueError('MAINT_MARGIN_RATE 1/MAX_LEVERAGE-dən kiçik olmalıdır (əks halda mövqe dərhal likvid olur).')
        if self.signal_exits not in ('auto', 'all', 'none'):
            raise ValueError('SIGNAL_EXITS auto, all və ya none olmalıdır.')
        if bool(self.user) != bool(self.password):
            raise ValueError('DASHBOARD_USER və DASHBOARD_PASSWORD birlikdə verilməlidir.')
        url = urlparse(self.binance_url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('BINANCE_URL https ünvanı olmalıdır.')

    @property
    def interval_ms(self) -> int:
        return TIMEFRAMES[self.timeframe]

    @property
    def db_path(self) -> Path:
        return self.data_dir / ('demo-futures.db' if self.demo else 'futures.db')

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        env = os.getenv
        return cls(
            symbols=tuple(dict.fromkeys(s.strip().upper() for s in env('SYMBOLS', 'ALL').split(',') if s.strip())),
            top_n=int(env('TOP_N', '50')), timeframe=env('TIMEFRAME', '1h').strip(),
            capital=float(env('START_CAPITAL', '2000')), margin_pct=float(env('ORDER_MARGIN_PCT', '5')),
            min_margin=float(env('MIN_MARGIN_USDT', '5')), take_profit=float(env('TAKE_PROFIT_USDT', '0')),
            tp_r_multiple=float(env('TP_R_MULTIPLE', '1.5')), risk_per_move=float(env('RISK_PER_MOVE', '0.25')),
            stop_loss_pct=float(env('STOP_LOSS_PCT', '50')),
            max_leverage=int(env('MAX_LEVERAGE', '100')),
            max_cost_r=float(env('MAX_COST_R', '0.10')), signal_exits=env('SIGNAL_EXITS', 'all').strip().lower(),
            cooldown_bars=int(env('COOLDOWN_BARS', '0')), max_hold_hours=float(env('MAX_HOLD_HOURS', '0')),
            fee_rate=float(env('FEE_RATE', '0.0005')), slippage_rate=float(env('SLIPPAGE_RATE', '0.0005')),
            maint_margin_rate=float(env('MAINT_MARGIN_RATE', '0.005')),
            history_bars=int(env('HISTORY_BARS', '500')),
            price_seconds=int(env('PRICE_REFRESH_SECONDS', '15')),
            candle_delay_seconds=int(env('CANDLE_DELAY_SECONDS', '5')),
            kline_parallelism=int(env('KLINE_PARALLELISM', '6')),
            data_dir=Path(env('DATA_DIR', 'data')),
            binance_url=env('BINANCE_URL', 'https://fapi.binance.com/fapi/v1').rstrip('/'),
            user=env('DASHBOARD_USER', ''), password=env('DASHBOARD_PASSWORD', ''),
            demo=env('DEMO_MODE', 'false').strip().lower() == 'true',
            demo_candle_seconds=float(env('DEMO_CANDLE_SECONDS', '20')),
        )
