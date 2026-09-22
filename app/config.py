import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default='', repr=False)
    model: str = 'jev-latest'
    symbols: tuple[str, ...] = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT')
    scan_seconds: int = 300
    min_confidence: float = .85
    max_spread: float = 15
    max_funding: float = .0003
    data_dir: Path = Path('data')
    user: str = ''
    password: str = field(default='', repr=False)
    demo: bool = False
    bridge_token: str = field(default='', repr=False)
    signal_ttl: int = 120
    freqtrade_url: str = 'http://127.0.0.1:8083'
    freqtrade_user: str = ''
    freqtrade_password: str = field(default='', repr=False)

    def __post_init__(self):
        if not self.symbols or len(self.symbols) > 20 or any(not re.fullmatch(r'[A-Z0-9]{3,20}USDT', s) for s in self.symbols):
            raise ValueError('SYMBOLS: 1–20 USDT simvolu tələb olunur.')
        if self.scan_seconds < 60:
            raise ValueError('SCAN_SECONDS >= 60 olmalıdır.')
        if not 0 <= self.min_confidence <= 1 or not 0 < self.max_spread <= 100 or not 0 <= self.max_funding <= .01:
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
    def load(cls):
        load_dotenv()
        return cls(api_key=os.getenv('TYPESAFE_API_KEY', ''), model=os.getenv('TYPESAFE_MODEL', 'jev-latest'),
                   symbols=tuple(dict.fromkeys(s.strip().upper() for s in os.getenv('SYMBOLS', 'BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT').split(',') if s.strip())),
                   scan_seconds=int(os.getenv('SCAN_SECONDS', '300')),
                   min_confidence=float(os.getenv('MIN_AI_CONFIDENCE', '.85')), max_spread=float(os.getenv('MAX_SPREAD_BPS', '15')),
                   max_funding=float(os.getenv('MAX_FUNDING_RATE', '.0003')), data_dir=Path(os.getenv('DATA_DIR', 'data')),
                   user=os.getenv('DASHBOARD_USER', ''), password=os.getenv('DASHBOARD_PASSWORD', ''),
                   demo=os.getenv('DEMO_MODE', 'false').lower() == 'true',
                   bridge_token=os.getenv('BRIDGE_TOKEN', ''), signal_ttl=int(os.getenv('SIGNAL_TTL_SECONDS', '120')),
                   freqtrade_url=os.getenv('FREQTRADE_URL', 'http://127.0.0.1:8083'),
                   freqtrade_user=os.getenv('FREQTRADE_USER', ''), freqtrade_password=os.getenv('FREQTRADE_PASSWORD', ''))
