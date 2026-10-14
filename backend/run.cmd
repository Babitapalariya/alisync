@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
python -m uvicorn alisync_app.main:app --host 127.0.0.1 --port 8002 --reload --log-level debug
echo AliSync backend stopped with exit code %ERRORLEVEL%
pause
