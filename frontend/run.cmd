@echo off
cd /d "%~dp0"
node server.js
echo AliSync frontend stopped with exit code %ERRORLEVEL%
pause
