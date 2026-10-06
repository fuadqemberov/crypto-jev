@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
rem The supervisor exits with code 3 when the UI asks for a restart, reset, git pull or branch switch;
rem the loop then reinstalls dependencies and upgrades the config so the new code starts cleanly.
:start
.venv\Scripts\python.exe -m pip install -q -e ".[paper]"
if errorlevel 1 exit /b 1
if not exist user_data\config.paper.json .venv\Scripts\python.exe -m app.paper
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m app.paper --upgrade
if errorlevel 1 exit /b 1
echo Only virtual trading. Configure TYPESAFE_API_KEY in .env; use the dashboard restart button after editing it.
.venv\Scripts\python.exe -m app.supervisor
if %errorlevel%==3 goto start
