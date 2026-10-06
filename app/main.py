from __future__ import annotations
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from starlette.responses import Response
import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic
from . import admin, supervisor
from .config import Settings
from .engine import Engine
from .store import Store

STATIC = Path(__file__).parent / 'static'


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.data_dir / ('demo.db' if settings.demo else 'analysis.db'))
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False) as client:
            app.state.engine = Engine(settings, client, store)
            app.state.manual_task = None
            app.state.market_views = {}
            app.state.last_manual = -float('inf')
            worker = asyncio.create_task(app.state.engine.run()) if start_worker else None
            telemetry = asyncio.create_task(app.state.engine.execution.run()) if start_worker else None
            try:
                yield
            finally:
                for task in (worker, telemetry, app.state.manual_task):
                    if task:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                store.close()

    app = FastAPI(title='Crypto Radar', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    @app.middleware('http')
    async def protect(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        bridge_request = request.url.path in ('/api/execution/signals', '/api/execution/heartbeat', '/api/execution/health')
        if bridge_request:
            expected = 'Bearer ' + settings.bridge_token
            if not settings.bridge_token or not secrets.compare_digest(request.headers.get('Authorization', '').encode(), expected.encode()):
                return JSONResponse({'detail': 'Bridge token tələb olunur.'}, status_code=401)
        elif settings.user:
            try:
                credentials = await basic(request)
            except HTTPException:
                credentials = None
            if not credentials or not (secrets.compare_digest(credentials.username.encode(), settings.user.encode()) &
                                        secrets.compare_digest(credentials.password.encode(), settings.password.encode())):
                return JSONResponse({'detail': 'Giriş tələb olunur.'}, status_code=401, headers={'WWW-Authenticate': 'Basic'})
        if request.method == 'POST' and not bridge_request:
            # Browser cross-site form submissions cannot supply this custom header.
            if request.headers.get('X-Crypto-Radar') != '1' or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return JSONResponse({'detail': 'Sorğu mənbəyi qəbul edilmədi.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    async def index() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    @app.get('/static/{filename}')
    async def static(filename: str) -> FileResponse:
        if filename not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(STATIC / filename)

    @app.get('/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/api/status')
    async def status() -> dict[str, Any]:
        return app.state.engine.status()

    @app.get('/api/history')
    async def history() -> list[dict[str, Any]]:
        return app.state.engine.store.history()

    @app.get('/api/market/{symbol}')
    async def market_view(symbol: str, interval: str = '15m') -> dict[str, Any]:
        engine = app.state.engine
        if symbol not in engine.symbols or interval not in ('1m', '15m', '1h', '4h'):
            raise HTTPException(404, 'Bazar və ya period tapılmadı.')
        if settings.demo:
            raise HTTPException(503, 'Demo rejimində canlı order book yoxdur.')
        key = (symbol, interval)
        cached = app.state.market_views.get(key)
        if cached and time.monotonic() - cached[0] < 5:
            return cached[1]
        try:
            bars, book, premium = await asyncio.gather(
                engine.market.get('/fapi/v1/klines', symbol=symbol, interval=interval, limit=100),
                engine.market.get('/fapi/v1/depth', symbol=symbol, limit=20),
                engine.market.get('/fapi/v1/premiumIndex', symbol=symbol))
            value = dict(symbol=symbol, interval=interval, observed_at=int(time.time()*1000),
                         candles=[dict(time=b[0], open=float(b[1]), high=float(b[2]), low=float(b[3]),
                                       close=float(b[4]), volume=float(b[5])) for b in bars],
                         bids=book['bids'], asks=book['asks'], mark=float(premium['markPrice']),
                         funding_rate=float(premium['lastFundingRate']))
            app.state.market_views[key] = (time.monotonic(), value)
            if len(app.state.market_views) > 8:
                del app.state.market_views[next(iter(app.state.market_views))]
            return value
        except Exception:
            raise HTTPException(503, 'Canlı bazar məlumatı alınmadı.') from None

    @app.get('/api/execution/signals')
    async def signals() -> dict[str, Any]:
        engine = app.state.engine
        await engine.refresh_marks()
        engine.execution.last_pull = int(time.time()*1000)
        return engine.signals()

    @app.post('/api/execution/heartbeat')
    async def heartbeat(request: Request) -> dict[str, bool]:
        # Bound streamed body before JSON parsing; content-length is untrusted.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256_000:
                raise HTTPException(413)
        try:
            import json
            app.state.engine.execution.receive_heartbeat(json.loads(body))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Heartbeat yoxlamadan keçmədi.') from None
        return {'accepted': True}

    @app.get('/api/execution/health')
    async def bridge_health() -> JSONResponse:
        engine = app.state.engine
        result = engine.signals()['diagnostics']
        healthy = result['health'] == 'healthy' and not any(code in result['blocks'] for code in ('storage_error', 'discovery', 'worker_error'))
        return JSONResponse(result, status_code=200 if healthy else 503)

    @app.post('/api/execution/{action}')
    async def control(action: str) -> dict[str, bool]:
        if action not in ('pause', 'resume'):
            raise HTTPException(404)
        app.state.engine.store.set_paused(action == 'pause')
        return {'paused': action == 'pause'}

    @app.post('/api/scan', status_code=202)
    async def scan() -> dict[str, str]:
        task = app.state.manual_task
        if app.state.engine.scanning or (task and not task.done()):
            raise HTTPException(409, 'Skan artıq davam edir.')
        if time.monotonic() - app.state.last_manual < 60:
            raise HTTPException(429, 'Növbəti skan üçün 60 saniyə gözləyin.')
        app.state.last_manual = time.monotonic()
        app.state.manual_task = asyncio.create_task(app.state.engine.scan())
        return {'message': 'Skan başladıldı.'}

    root = Path(__file__).resolve().parent.parent

    def supervised() -> None:
        if os.environ.get(supervisor.ENV_FLAG) != '1':
            raise HTTPException(409, 'Tətbiq supervisor ilə işə salınmayıb; run.cmd və ya run-paper.cmd ilə başladın.')

    @app.get('/api/admin/info')
    async def admin_info(fetch: bool = False) -> dict[str, Any]:
        try:
            value = await admin.info(root, fetch)
        except admin.GitError as exc:
            value = {'error': str(exc)}
        return {**value, 'supervised': os.environ.get(supervisor.ENV_FLAG) == '1', 'wallet': supervisor.WALLET}

    @app.post('/api/admin/{action}', status_code=202)
    async def admin_action(action: str, request: Request) -> dict[str, Any]:
        if action not in ('restart', 'reset', 'pull', 'switch'):
            raise HTTPException(404)
        supervised()
        output = ''
        try:
            if action == 'pull':
                output = await admin.pull(root)
            elif action == 'switch':
                body = await request.json()
                output = await admin.switch(root, str(body.get('branch', '')) if isinstance(body, dict) else '')
        except admin.GitError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError:
            raise HTTPException(422, 'Sorğu formatı etibarsızdır.') from None
        if action == 'pull' and 'Already up to date' in output:
            return {'restarting': False, 'output': 'Artıq aktualdır; restart lazım deyil.'}
        # Pulled or switched code only takes effect after a restart, so git actions restart too.
        supervisor.request(settings.data_dir, 'reset' if action == 'reset' else 'restart')
        return {'restarting': True, 'output': output[-2000:]}

    return app


app = create_app()
