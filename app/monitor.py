"""Optional external heartbeat check; exits nonzero for an unhealthy paper executor.

Run from a timer/monitor. No trading actions and no secrets in command arguments/output.
"""
import sys
import httpx
from .config import Settings


def main() -> int:
    settings = Settings.load()
    if not settings.bridge_token:
        print('UNHEALTHY: bridge token is not configured')
        return 1
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = client.get('http://127.0.0.1:8082/api/execution/health',
                                  headers={'Authorization': 'Bearer ' + settings.bridge_token})
            if response.status_code != 200 or response.json().get('health') != 'healthy':
                raise ValueError('unhealthy')
        print('HEALTHY: engine and paper heartbeat are fresh')
        return 0
    except (httpx.HTTPError, ValueError):
        print('UNHEALTHY: engine or paper executor heartbeat unavailable')
        return 1


if __name__ == '__main__':
    sys.exit(main())
