@echo off
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\daybook-autostart.cmd"
set "RAW=https://raw.githubusercontent.com/LMC4S/daybook/main"

if /i "%~1"=="install" goto install
if /i "%~1"=="uninstall" goto uninstall
if /i "%~1"=="update" goto update
goto run

:install
> "%STARTUP%" echo @call "%~f0"
echo Daybook will now start automatically when you log in.
echo To undo: daybook.cmd uninstall
goto :eof

:uninstall
del "%STARTUP%" 2>nul
echo Autostart removed.
goto :eof

:update
echo Downloading the latest app.py ...
powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing '%RAW%/app.py' -OutFile '%~dp0app.py.new'"
if not exist "%~dp0app.py.new" (
  echo Update failed - could not download. Check your network and try again.
  goto :eof
)
move /y "%~dp0app.py.new" "%~dp0app.py" >nul
echo Updated. Restarting Daybook ...
goto run

:run
cd /d "%~dp0"

rem Always stop any running instance first, so what starts is the current code.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>nul

rem Prefer pythonw (no console window); fall back to a minimized console.
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0app.py" --no-browser
) else (
  start "Daybook" /min python "%~dp0app.py" --no-browser
)
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
endlocal
