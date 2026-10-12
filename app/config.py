"""Runtime settings for the 20-strategy live paper-trading system (read from .env)."""
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
    max_positions: int = 10
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    min_order: float = 10.0
    history_bars: int = 500
    price_seconds: int = 15
    candle_delay_seconds: int = 5
    kline_parallelism: int = 6
    data_dir: Path = Path('data')
    binance_url: str = 'https://data-api.binance.vision/api/v3'
    user: str = ''
    password: str = field(default='', repr=False)
    demo: bool = False
    demo_candle_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.symbols != ('ALL',) and (not self.symbols or any(not re.fullmatch(r'[A-Z0-9]{2,20}USDT', s) for s in self.symbols)):
            raise ValueError('SYMBOLS: ALL və ya vergüllə ayrılmış USDT cütləri (məs. BTCUSDT,ETHUSDT) olmalıdır.')
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f'TIMEFRAME bunlardan biri olmalıdır: {", ".join(TIMEFRAMES)}')
        for name, low, high in [('top_n', 1, 500), ('max_positions', 1, 50), ('history_bars', 250, 1000),
                                ('price_seconds', 5, 300), ('candle_delay_seconds', 0, 120), ('kline_parallelism', 1, 16)]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name.upper()} {low}–{high} aralığında tam ədəd olmalıdır.')
        for name, low, high in [('capital', 10, 10_000_000), ('fee_rate', 0, 0.01), ('slippage_rate', 0, 0.01),
                                ('min_order', 1, 10_000), ('demo_candle_seconds', 1, 3600)]:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name.upper()} {low}–{high} aralığında olmalıdır.')
        if self.min_order * self.max_positions > self.capital:
            raise ValueError('MIN_ORDER_USDT × MAX_POSITIONS başlanğıc kapitaldan böyük ola bilməz.')
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
        return self.data_dir / ('demo.db' if self.demo else 'strategies.db')

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        env = os.getenv
        return cls(
            symbols=tuple(dict.fromkeys(s.strip().upper() for s in env('SYMBOLS', 'ALL').split(',') if s.strip())),
            top_n=int(env('TOP_N', '50')), timeframe=env('TIMEFRAME', '1h').strip(),
            capital=float(env('START_CAPITAL', '2000')), max_positions=int(env('MAX_POSITIONS', '10')),
            fee_rate=float(env('FEE_RATE', '0.001')), slippage_rate=float(env('SLIPPAGE_RATE', '0.0005')),
            min_order=float(env('MIN_ORDER_USDT', '10')), history_bars=int(env('HISTORY_BARS', '500')),
            price_seconds=int(env('PRICE_REFRESH_SECONDS', '15')),
            candle_delay_seconds=int(env('CANDLE_DELAY_SECONDS', '5')),
            kline_parallelism=int(env('KLINE_PARALLELISM', '6')),
            data_dir=Path(env('DATA_DIR', 'data')),
            binance_url=env('BINANCE_URL', 'https://data-api.binance.vision/api/v3').rstrip('/'),
            user=env('DASHBOARD_USER', ''), password=env('DASHBOARD_PASSWORD', ''),
            demo=env('DEMO_MODE', 'false').strip().lower() == 'true',
            demo_candle_seconds=float(env('DEMO_CANDLE_SECONDS', '20')),
        )
