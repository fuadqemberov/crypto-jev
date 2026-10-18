"""Dashboard + engine in one process: uvicorn app.main:app --workers 1"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic
from starlette.responses import Response

from .config import Settings
from .demo import DemoMarket
from .engine import Engine
from .market import BinanceMarket
from .store import Store

STATIC = Path(__file__).parent / 'static'
CONTROL_PATH = '/api/control/entries'
CHART_JS = Path(__file__).parent.parent / 'backtest' / 'assets' / 'chart.umd.min.js'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(settings: Settings | None = None, start_worker: bool = True, market: Any = None) -> FastAPI:
    settings = settings or Settings.load()
    app_log = logging.getLogger('app')
    if not app_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        app_log.addHandler(handler)
    app_log.setLevel(logging.INFO)
    app_log.propagate = False

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path)
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False) as client:
            source = market or (DemoMarket(settings) if settings.demo else BinanceMarket(settings, client))
            app.state.engine = Engine(settings, source, store)
            worker = asyncio.create_task(app.state.engine.run()) if start_worker else None
            try:
                yield
            finally:
                if worker:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker
                store.close()

    app = FastAPI(title='Crypto Strategies', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    @app.middleware('http')
    async def protect(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if settings.user and request.url.path != '/health':
            try:
                credentials = await basic(request)
            except HTTPException:
                credentials = None
            if not credentials or not (secrets.compare_digest(credentials.username.encode(), settings.user.encode()) &
                                        secrets.compare_digest(credentials.password.encode(), settings.password.encode())):
                return JSONResponse({'detail': 'Giriş tələb olunur.'}, status_code=401, headers={'WWW-Authenticate': 'Basic'})
        if request.method not in ('GET', 'HEAD'):
            # Read-only except one switch: pausing / resuming NEW entries. Balances, positions and
            # trades can never be changed over HTTP. The custom header forces a CORS pre-flight
            # (never granted) and the Origin must be this host, so other sites cannot press it.
            if request.method != 'POST' or request.url.path != CONTROL_PATH:
                return JSONResponse({'detail': 'Yalnız oxuma icazəsi var.'}, status_code=405)
            origin, host = request.headers.get('origin'), request.headers.get('host')
            if request.headers.get('x-dashboard') != '1' or (origin and urlsplit(origin).netloc != host):
                return JSONResponse({'detail': 'Sorğu rədd edildi.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = CSP
        return response

    @app.get('/')
    async def index() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    @app.get('/static/{filename}')
    async def static(filename: str) -> FileResponse:
        if filename == 'chart.umd.min.js':
            return FileResponse(CHART_JS, media_type='text/javascript')
        if filename not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(STATIC / filename)

    @app.get('/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/api/overview')
    async def overview() -> dict[str, Any]:
        return app.state.engine.overview()

    @app.get('/api/curves')
    async def curves() -> dict[str, Any]:
        return app.state.engine.curves()

    @app.post(CONTROL_PATH)
    async def entries(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        paused = payload.get('paused')
        if not isinstance(paused, bool):
            raise HTTPException(422, "'paused' true və ya false olmalıdır.")
        return app.state.engine.set_entries_paused(paused)

    @app.get('/api/strategy/{key}')
    async def strategy(key: str) -> dict[str, Any]:
        if key not in app.state.engine.by_key:
            raise HTTPException(404, 'Strategiya tapılmadı.')
        return app.state.engine.detail(key)

    return app


app = create_app()
