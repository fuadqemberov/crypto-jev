@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
if not exist .env copy .env.example .env
rem The supervisor exits with code 3 when the UI asks for a restart, reset, git pull or branch switch;
rem the loop then reinstalls dependencies and upgrades the config so the new code starts cleanly.
:start
.venv\Scripts\python.exe -m pip install -q -r requirements.lock
if errorlevel 1 exit /b 1
if exist user_data\config.paper.json .venv\Scripts\python.exe -m app.paper --upgrade
if errorlevel 1 exit /b 1
rem Freqtrade virtual executor runs only when installed and configured; without it no order is ever opened.
.venv\Scripts\python.exe -m app.supervisor
if %errorlevel%==3 goto start
