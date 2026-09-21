import os
import re
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

    def __post_init__(self):
        if not self.symbols or len(self.symbols) > 20 or any(not re.fullmatch(r'[A-Z0-9]{3,20}USDT', s) for s in self.symbols):
            raise ValueError('SYMBOLS: 1–20 USDT simvolu tələb olunur.')
        if self.scan_seconds < 60:
            raise ValueError('SCAN_SECONDS >= 60 olmalıdır.')
        if not 0 <= self.min_confidence <= 1 or not 0 < self.max_spread <= 100 or not 0 <= self.max_funding <= .01:
            raise ValueError('Risk parametrləri etibarsızdır.')
        if bool(self.user) != bool(self.password):
            raise ValueError('DASHBOARD_USER və DASHBOARD_PASSWORD birlikdə verilməlidir.')

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(api_key=os.getenv('TYPESAFE_API_KEY', ''), model=os.getenv('TYPESAFE_MODEL', 'jev-latest'),
                   symbols=tuple(dict.fromkeys(s.strip().upper() for s in os.getenv('SYMBOLS', 'BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT').split(',') if s.strip())),
                   scan_seconds=int(os.getenv('SCAN_SECONDS', '300')),
                   min_confidence=float(os.getenv('MIN_AI_CONFIDENCE', '.85')), max_spread=float(os.getenv('MAX_SPREAD_BPS', '15')),
                   max_funding=float(os.getenv('MAX_FUNDING_RATE', '.0003')), data_dir=Path(os.getenv('DATA_DIR', 'data')),
                   user=os.getenv('DASHBOARD_USER', ''), password=os.getenv('DASHBOARD_PASSWORD', ''),
                   demo=os.getenv('DEMO_MODE', 'false').lower() == 'true')
