@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.lock
if errorlevel 1 exit /b 1
if not exist .env copy .env.example .env
rem Freqtrade virtual executor in its own window; without it no order is ever opened.
if exist user_data\config.paper.json .venv\Scripts\python.exe -c "import freqtrade" 2>nul && start "Crypto Jev Freqtrade" .venv\Scripts\python.exe -m freqtrade trade --config user_data/config.paper.json --strategy-path user_data/strategies
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
