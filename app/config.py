from __future__ import annotations

import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv
from .risk import RiskPolicy, finite


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default='', repr=False)
    model: str = 'jev-latest'
    symbols: tuple[str, ...] = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT')
    scan_seconds: int = 300
    performance_profile: str = 'balanced'
    priority_seconds: int | None = None
    radar_parallelism: int | None = None
    priority_parallelism: int | None = None
    priority_size: int = 24
    recent_confidence_seconds: int = 900
    symbol_timeout: int = 35
    ai_cache_size: int = 3000
    ai_cache_ttl: int = 300
    ai_cache_persistent: bool = False
    close_confidence: float = .85
    leverage_confidence: float = .90
    bridge_poll_seconds: int = 1
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    min_confidence: float = .90
    max_spread: float = 15
    max_funding: float = .0003
    data_dir: Path = Path('data')
    user: str = ''
    password: str = field(default='', repr=False)
    demo: bool = False
    bridge_token: str = field(default='', repr=False)
    signal_ttl: int | None = None
    freqtrade_url: str = 'http://127.0.0.1:8083'
    freqtrade_user: str = ''
    freqtrade_password: str = field(default='', repr=False)

    def __post_init__(self) -> None:
        profiles = {'conservative': (30, 2, 1, 180), 'balanced': (15, 4, 2, 120), 'aggressive': (10, 6, 3, 90)}
        if self.performance_profile not in profiles:
            raise ValueError('Unknown PERFORMANCE_PROFILE')
        for name, value in zip(('priority_seconds', 'radar_parallelism', 'priority_parallelism', 'signal_ttl'), profiles[self.performance_profile]):
            if getattr(self, name) is None:
                object.__setattr__(self, name, value)
        for name, low, high in [('priority_seconds', 5, 60), ('radar_parallelism', 1, 16),
                ('priority_parallelism', 1, 8), ('priority_size', 1, 200), ('recent_confidence_seconds', 60, 3600),
                ('symbol_timeout', 5, 60), ('ai_cache_size', 1, 50000), ('ai_cache_ttl', 1, 900),
                ('bridge_poll_seconds', 1, 5), ('scan_seconds', 60, 86400), ('signal_ttl', 30, 300)]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid setting: {name}')
        if self.priority_seconds + self.symbol_timeout >= self.signal_ttl:
            raise ValueError('Priority interval + analysis timeout must be below signal TTL')
        for value in (self.close_confidence, self.leverage_confidence):
            if not finite(value) or not 0 <= value <= 1:
                raise ValueError('Invalid confidence threshold')

        if self.symbols != ('ALL',) and (not self.symbols or any(not re.fullmatch(r'[A-Z0-9]+USDT', s) for s in self.symbols)):
            raise ValueError('SYMBOLS: ALL və ya USDT simvolları tələb olunur.')
        if self.scan_seconds < 60:
            raise ValueError('SCAN_SECONDS >= 60 olmalıdır.')
        if not all(finite(v) for v in (self.min_confidence, self.max_spread, self.max_funding)) or not 0 <= self.min_confidence <= 1 or not 0 < self.max_spread <= 100 or not 0 <= self.max_funding <= .01:
            raise ValueError('Risk parametrləri etibarsızdır.')
        if bool(self.user) != bool(self.password):
            raise ValueError('DASHBOARD_USER və DASHBOARD_PASSWORD birlikdə verilməlidir.')
        if self.bridge_token and len(self.bridge_token) < 32:
            raise ValueError('BRIDGE_TOKEN minimum 32 simvol olmalıdır.')
        if not 30 <= self.signal_ttl <= 300:
            raise ValueError('SIGNAL_TTL_SECONDS 30–300 olmalıdır.')
        url = urlparse(self.freqtrade_url)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('FREQTRADE_URL lokal HTTP ünvanı olmalıdır.')
        if bool(self.freqtrade_user) != bool(self.freqtrade_password):
            raise ValueError('Freqtrade istifadəçi və parolu birlikdə verilməlidir.')

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        optional_int = lambda key: int(os.environ[key]) if os.getenv(key) else None
        return cls(api_key=os.getenv('TYPESAFE_API_KEY', ''), model=os.getenv('TYPESAFE_MODEL', 'jev-latest'),
                   symbols=tuple(dict.fromkeys(s.strip().upper() for s in os.getenv('SYMBOLS', 'ALL').split(',') if s.strip())),
                   scan_seconds=int(os.getenv('SCAN_SECONDS', '300')),
                   performance_profile=os.getenv('PERFORMANCE_PROFILE', 'balanced'),
                   priority_seconds=optional_int('PRIORITY_SCAN_SECONDS'), radar_parallelism=optional_int('RADAR_PARALLELISM'),
                   priority_parallelism=optional_int('PRIORITY_PARALLELISM'), priority_size=int(os.getenv('PRIORITY_SIZE', '24')),
                   symbol_timeout=int(os.getenv('SYMBOL_TIMEOUT_SECONDS', '35')),
                   recent_confidence_seconds=int(os.getenv('RECENT_CONFIDENCE_SECONDS', '900')),
                   ai_cache_size=int(os.getenv('AI_CACHE_SIZE', '3000')), ai_cache_ttl=int(os.getenv('AI_CACHE_TTL_SECONDS', '300')),
                   ai_cache_persistent=os.getenv('AI_CACHE_PERSISTENT', 'false').lower() == 'true',
                   close_confidence=float(os.getenv('MIN_CLOSE_CONFIDENCE', '.85')),
                   leverage_confidence=float(os.getenv('MIN_LEVERAGE_CONFIDENCE', '.90')),
                   bridge_poll_seconds=int(os.getenv('BRIDGE_POLL_SECONDS', '1')),
                   risk=RiskPolicy(capital_risk=float(os.getenv('CAPITAL_RISK', '.005')),
                       margin_fraction=float(os.getenv('MAX_MARGIN_FRACTION', '.07')),
                       margin_loss=float(os.getenv('MAX_MARGIN_LOSS', '.50')),
                       max_leverage=int(os.getenv('MAX_LEVERAGE', '20')),
                       daily_loss=float(os.getenv('DAILY_LOSS_FRACTION', '.03')),
                       min_rr=float(os.getenv('MIN_NET_RR', '1.5')), max_spread=float(os.getenv('MAX_SPREAD_BPS', '15')),
                       max_slippage=float(os.getenv('MAX_ENTRY_SLIPPAGE', '.003')),
                       cost_per_side=float(os.getenv('COST_PER_SIDE', '.0008')),
                       funding_periods=int(os.getenv('FUNDING_RESERVE_PERIODS', '1')),
                       cooldown_seconds=int(os.getenv('COOLDOWN_SECONDS', '300'))),
                   min_confidence=float(os.getenv('MIN_AI_CONFIDENCE', '.90')), max_spread=float(os.getenv('MAX_SPREAD_BPS', '15')),
                   max_funding=float(os.getenv('MAX_FUNDING_RATE', '.0003')), data_dir=Path(os.getenv('DATA_DIR', 'data')),
                   user=os.getenv('DASHBOARD_USER', ''), password=os.getenv('DASHBOARD_PASSWORD', ''),
                   demo=os.getenv('DEMO_MODE', 'false').lower() == 'true',
                   bridge_token=os.getenv('BRIDGE_TOKEN', ''), signal_ttl=optional_int('SIGNAL_TTL_SECONDS'),
                   freqtrade_url=os.getenv('FREQTRADE_URL', 'http://127.0.0.1:8083'),
                   freqtrade_user=os.getenv('FREQTRADE_USER', ''), freqtrade_password=os.getenv('FREQTRADE_PASSWORD', ''))
