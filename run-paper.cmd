@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install -e ".[paper]"
if errorlevel 1 exit /b 1
if not exist user_data\config.paper.json .venv\Scripts\python.exe -m app.paper
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m app.paper --upgrade
if errorlevel 1 exit /b 1
echo Only virtual trading. Configure TYPESAFE_API_KEY in .env; restart the dashboard after editing it.
start "Crypto Jev Dashboard" .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
.venv\Scripts\python.exe -m freqtrade trade --config user_data/config.paper.json --strategy-path user_data/strategies
