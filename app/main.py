import asyncio
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic
from .config import Settings
from .engine import Engine
from .store import Store

STATIC = Path(__file__).parent / 'static'


def create_app(settings=None, start_worker=True):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.data_dir / ('demo.db' if settings.demo else 'analysis.db'))
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False) as client:
            app.state.engine = Engine(settings, client, store)
            app.state.manual_task = None
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

    app = FastAPI(title='Crypto Jev', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    @app.middleware('http')
    async def protect(request: Request, call_next):
        bridge_request = request.url.path == '/api/execution/signals'
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
        if request.method == 'POST':
            # Browser cross-site form submissions cannot supply this custom header.
            if request.headers.get('X-Crypto-Jev') != '1' or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return JSONResponse({'detail': 'Sorğu mənbəyi qəbul edilmədi.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/static/{filename}')
    async def static(filename: str):
        if filename not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(STATIC / filename)

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/status')
    async def status():
        return app.state.engine.status()

    @app.get('/api/history')
    async def history():
        return app.state.engine.store.history()

    @app.get('/api/execution/signals')
    async def signals():
        engine = app.state.engine
        return engine.execution.signals(engine.rows.values(), engine.storage_error)

    @app.post('/api/execution/{action}')
    async def control(action: str):
        if action not in ('pause', 'resume'):
            raise HTTPException(404)
        app.state.engine.store.set_paused(action == 'pause')
        return {'paused': action == 'pause'}

    @app.post('/api/scan', status_code=202)
    async def scan():
        task = app.state.manual_task
        if app.state.engine.scanning or (task and not task.done()):
            raise HTTPException(409, 'Skan artıq davam edir.')
        if time.monotonic() - app.state.last_manual < 60:
            raise HTTPException(429, 'Növbəti skan üçün 60 saniyə gözləyin.')
        app.state.last_manual = time.monotonic()
        app.state.manual_task = asyncio.create_task(app.state.engine.scan())
        return {'message': 'Skan başladıldı.'}

    return app


app = create_app()
