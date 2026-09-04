@echo off
setlocal enabledelayedexpansion
title Chart Viewer Client Windows
echo ========================================================
echo       TC2000-Style Desktop Chart Viewer Client
echo ========================================================
echo.

set PY_CMD=

REM 1. Check if Windows py launcher is available
py -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    goto :found_python
)

REM 2. Check if python is available
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :found_python
)

REM 3. Search common user install directory
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        set "PY_CMD=%%D\python.exe"
        goto :found_python
    )
)

REM 4. Search Program Files
for /d %%D in ("C:\Program Files\Python3*") do (
    if exist "%%D\python.exe" (
        set "PY_CMD=%%D\python.exe"
        goto :found_python
    )
)

echo ========================================================
echo FEHLER: Kein funktionierendes Python auf Windows gefunden.
echo ========================================================
echo.
echo Der Befehl 'python' verweist auf den Microsoft Store Alias.
echo.
echo Option 1: Schnellinstallation in der Windows PowerShell:
echo   winget install Python.Python.3.12
echo.
echo Option 2: Von der offiziellen Website herunterladen:
echo   https://www.python.org/downloads/
echo   (WICHTIG: Beim Setup den Haken bei 'Add python.exe to PATH' setzen!)
echo.
pause
exit /b 1

:found_python
echo Verwende Python: %PY_CMD%
echo Verbinde mit Agent Server auf Linux (ws://10.20.0.23:8765)...
echo.

%PY_CMD% -c "import PySide6, msgspec, websockets" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :run_app

:install_deps
echo Installiere erforderliche Pakete (PySide6, msgspec, websockets)...
%PY_CMD% -m pip install PySide6 msgspec websockets
if errorlevel 1 (
    echo.
    echo Fehler bei der Installation der Abhängigkeiten.
    pause
    exit /b 1
)

:run_app
set PYTHONPATH=%~dp0src;%PYTHONPATH%
%PY_CMD% "%~dp0src\chart_viewer\run_viewer.py" --ws ws://10.20.0.23:8765

echo.
echo Viewer-Prozess beendet (Exit-Code: %ERRORLEVEL%).
pause
