"""Generate local paper-only Freqtrade configuration without committing credentials."""
import json
import os
import secrets
import argparse
import shutil
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from .config import Settings
from .execution import pair_for


def build_config(settings, token, username, password):
    return {
        '$schema': 'https://schema.freqtrade.io/schema.json',
        'strategy': 'JevBridgeStrategy', 'timeframe': '1m',
        'dry_run': True, 'dry_run_wallet': 2000, 'fee': .0005,
        'trading_mode': 'futures', 'margin_mode': 'isolated',
        'max_open_trades': -1, 'stake_currency': 'USDT', 'stake_amount': 140,
        'stoploss': -.50, 'liquidation_buffer': .10,
        'tradable_balance_ratio': 1.0, 'fiat_display_currency': '',
        'cancel_open_orders_on_exit': True,
        'unfilledtimeout': {'entry': 1, 'exit': 1, 'unit': 'minutes'},
        'order_types': {'entry': 'market', 'exit': 'market', 'stoploss': 'market', 'stoploss_on_exchange': False},
        'order_time_in_force': {'entry': 'GTC', 'exit': 'GTC'},
        'entry_pricing': {'price_side': 'other', 'use_order_book': True, 'order_book_top': 1,
                          'check_depth_of_market': {'enabled': False, 'bids_to_ask_delta': 1}},
        'exit_pricing': {'price_side': 'other', 'use_order_book': True, 'order_book_top': 1},
        'exchange': {'name': 'binance', 'key': '', 'secret': '',
                     'ccxt_config': {'enableRateLimit': True}, 'ccxt_async_config': {},
                     'pair_whitelist': [pair_for(s) for s in settings.symbols], 'pair_blacklist': []},
        'pairlists': [{'method': 'StaticPairList'}],
        'telegram': {'enabled': False, 'token': '', 'chat_id': ''},
        'api_server': {'enabled': True, 'listen_ip_address': '127.0.0.1', 'listen_port': 8083,
                       'verbosity': 'error', 'enable_openapi': False, 'CORS_origins': [],
                       'username': username, 'password': password,
                       'jwt_secret_key': secrets.token_urlsafe(32), 'ws_token': secrets.token_urlsafe(32)},
        'bot_name': 'Crypto Jev Paper', 'initial_state': 'running', 'force_entry_enable': False,
        'internals': {'process_throttle_secs': 5},
        'db_url': 'sqlite:///data/freqtrade-paper.sqlite',
        'jev_bridge': {'url': 'http://127.0.0.1:8082', 'token': token},
    }


def initialize(root=Path('.')):
    target = root / 'user_data' / 'config.paper.json'
    if target.exists():
        raise FileExistsError('user_data/config.paper.json mövcuddur; üzərinə yazılmadı.')
    env_path = root / '.env'
    original = env_path.read_text(encoding='utf-8') if env_path.exists() else (root / '.env.example').read_text(encoding='utf-8')
    existing = dotenv_values(env_path) if env_path.exists() else {}
    values = {
        'BRIDGE_TOKEN': existing.get('BRIDGE_TOKEN') or secrets.token_urlsafe(32),
        'FREQTRADE_USER': existing.get('FREQTRADE_USER') or 'cryptojev',
        'FREQTRADE_PASSWORD': existing.get('FREQTRADE_PASSWORD') or secrets.token_urlsafe(32),
        'FREQTRADE_URL': 'http://127.0.0.1:8083',
    }
    symbols = existing.get('SYMBOLS') or 'BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT'
    settings = Settings(symbols=tuple(s.strip().upper() for s in symbols.split(',') if s.strip()),
                        bridge_token=values['BRIDGE_TOKEN'])
    config = build_config(settings, values['BRIDGE_TOKEN'], values['FREQTRADE_USER'], values['FREQTRADE_PASSWORD'])
    # Replace only managed connection fields, preserving the user's API key and other settings.
    lines = [line for line in original.splitlines() if line.split('=', 1)[0].strip() not in values]
    lines.extend(f'{k}={json.dumps(v)}' for k, v in values.items())
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / 'data').mkdir(exist_ok=True)
    # Exclusive creation refuses to overwrite an existing config/database or reset a wallet.
    with target.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(config, indent=2) + '\n')
    env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if os.name != 'nt':
        target.chmod(0o600)
        env_path.chmod(0o600)
    return target


def upgrade(root=Path('.')):
    """Migrate only execution settings; preserve wallet DB, credentials and symbols."""
    target = root / 'user_data' / 'config.paper.json'
    config = json.loads(target.read_text(encoding='utf-8'))
    if config.get('dry_run') is not True or config.get('strategy') != 'JevBridgeStrategy':
        raise ValueError('Yalnız JevBridgeStrategy dry-run konfiqurasiyası yenilənə bilər.')
    changes = {'max_open_trades': -1, 'stake_amount': 140, 'stoploss': -.50}
    if all(config.get(k) == v for k, v in changes.items()):
        return target
    backup = target.with_name(target.name + '.backup-' + secrets.token_hex(4))
    shutil.copy2(target, backup)
    if os.name != 'nt':
        backup.chmod(0o600)
    config.update(changes)
    # Atomic replacement: an interrupted upgrade leaves the original or complete new config.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                     prefix='.config.paper.', suffix='.tmp', delete=False) as handle:
        temp = Path(handle.name)
        json.dump(config, handle, indent=2)
        handle.write('\n')
    os.replace(temp, target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--upgrade', action='store_true')
    args = parser.parse_args()
    try:
        target = upgrade() if args.upgrade else initialize()
        print(f'Hazır: {target} — yalnız virtual icra; JEV leverage, say limiti yoxdur. Açarlar göstərilmir.')
    except FileExistsError as exc:
        print(exc)
