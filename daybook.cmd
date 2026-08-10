@echo off
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\daybook-autostart.cmd"

if /i "%~1"=="install" (
  > "%STARTUP%" echo @call "%~f0"
  echo Daybook will now start automatically when you log in.
  echo To undo: daybook.cmd uninstall
  goto :eof
)

if /i "%~1"=="uninstall" (
  del "%STARTUP%" 2>nul
  echo Autostart removed.
  goto :eof
)

cd /d "%~dp0"

rem Already running? Then just open the page.
netstat -ano | findstr ":8765 " | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto open

rem Prefer pythonw (no console window); fall back to a minimized console.
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0app.py" --no-browser
) else (
  start "Daybook" /min python "%~dp0app.py" --no-browser
)
timeout /t 2 /nobreak >nul

:open
start "" http://localhost:8765
endlocal
