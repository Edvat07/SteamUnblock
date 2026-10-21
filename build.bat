@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Steam Unblock

set "OUT=%~dp0release"

rem --- Locate Python ---------------------------------------------------------
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")
)
%PY% --version >nul 2>&1 || (
    echo [ERROR] Python not found. Install Python 3.12 from python.org and run again.
    goto :fail
)

rem --- sing-box and wintun ---------------------------------------------------
if not exist "core\sing-box.exe" (
    echo [ERROR] core\sing-box.exe is missing. Download sing-box 1.14.x for windows-amd64 from
    echo         https://github.com/SagerNet/sing-box/releases and put sing-box.exe in core\
    goto :fail
)
if not exist "core\wintun.dll" (
    echo [ERROR] core\wintun.dll is missing. Get the amd64 DLL from https://www.wintun.net/
    goto :fail
)

rem --- Dependencies ----------------------------------------------------------
%PY% -m pip install --disable-pip-version-check -q pyinstaller pystray pillow || goto :fail

rem --- Build -----------------------------------------------------------------
echo [*] Building SteamUnblock.exe...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --noupx ^
    --name SteamUnblock --icon "%~dp0app_icon.ico" ^
    --distpath "%OUT%" --workpath "%~dp0build" --specpath "%~dp0build" ^
    steam_app.py || goto :fail

if not exist "%OUT%\core" mkdir "%OUT%\core"
copy /y "core\sing-box.exe" "%OUT%\core\" >nul || goto :fail
copy /y "core\wintun.dll" "%OUT%\core\" >nul || goto :fail
copy /y "app_icon.ico" "%OUT%\" >nul

rmdir /s /q "%~dp0build" 2>nul
echo.
echo [OK] Done: %OUT%\SteamUnblock.exe
pause
exit /b 0

:fail
echo.
echo [FAILED] See the error above.
pause
exit /b 1
