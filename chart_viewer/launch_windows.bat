@echo off
setlocal enabledelayedexpansion
title Chart Viewer Client Windows
echo ========================================================
echo       TC2000-Style Desktop Chart Viewer Client
echo ========================================================
echo.

set PY_CMD=

REM 1. Fast Python detection via native 'where' (no interpreter startup)
where py >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    goto :found_python
)

where python >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :found_python
)

REM Search common user install directory
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        set "PY_CMD=%%D\python.exe"
        goto :found_python
    )
)

REM Search Program Files
for /d %%D in ("C:\Program Files\Python3*") do (
    if exist "%%D\python.exe" (
        set "PY_CMD=%%D\python.exe"
        goto :found_python
    )
)

echo ========================================================
echo FEHLER: Kein funktionierendes Python auf Windows gefunden.
echo ========================================================
pause
exit /b 1

:found_python
echo Verwende Python: %PY_CMD%
echo.

REM --- Lokales Cache-Verzeichnis (lokale Platte, nicht Netzlaufwerk) ---
set "CACHE_DIR=%LOCALAPPDATA%\ChartViewer"
if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%"

REM --- 1. Auto-Sync from Linux Server (versioned - skip when unchanged) ---
echo [1/3] Pruefe Server-Version (http://10.20.0.23:8766)...
set "SYNC_NEEDED=1"
set "NEW_VER="

curl.exe -s -f -m 5 "http://10.20.0.23:8766/api/sync_version" -o "%CACHE_DIR%\sync_version.new" >nul 2>&1
if not errorlevel 1 (
    if exist "%CACHE_DIR%\sync_version.new" (
        set /p NEW_VER=<"%CACHE_DIR%\sync_version.new"
        del /f /q "%CACHE_DIR%\sync_version.new" >nul 2>&1
    )
)

if defined NEW_VER (
    set "OLD_VER="
    if exist "%CACHE_DIR%\sync_version" set /p OLD_VER=<"%CACHE_DIR%\sync_version"
    if "!NEW_VER!"=="!OLD_VER!" (
        echo [OK] Code bereits aktuell - Sync uebersprungen.
        set "SYNC_NEEDED=0"
    )
)

if "!SYNC_NEEDED!"=="1" (
    echo [1/3] Lade neueste Version vom Server...
    curl.exe -s -f -m 20 "http://10.20.0.23:8766/api/sync" -o "%CACHE_DIR%\src_bundle.tar"
    if not errorlevel 1 (
        tar.exe -xf "%CACHE_DIR%\src_bundle.tar" -C "%CACHE_DIR%"
        del /f /q "%CACHE_DIR%\src_bundle.tar" >nul 2>&1
        if defined NEW_VER (
            >"%CACHE_DIR%\sync_version" echo !NEW_VER!
        )
        echo [OK] Code erfolgreich auf den neuesten Stand aktualisiert!
    ) else (
        del /f /q "%CACHE_DIR%\src_bundle.tar" >nul 2>&1
        echo [INFO] Server-Sync uebersprungen - Server offline.
    )
)
echo.

REM --- 2. Check Dependencies (fast find_spec - avoids heavy PySide6 import) ---
echo [2/3] Pruefe Python-Abhaengigkeiten (PySide6, msgspec, websockets)...
%PY_CMD% -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','msgspec','websockets')) else 1)" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :run_app

:install_deps
echo Installiere erforderliche Pakete (PySide6, msgspec, websockets)...
%PY_CMD% -m pip install PySide6 msgspec websockets
if errorlevel 1 (
    echo.
    echo Fehler bei der Installation der Abhaengigkeiten.
    pause
    exit /b 1
)

:run_app
echo [3/3] Starte Desktop Chart Viewer Client...
echo Verbinde mit ws://10.20.0.23:8765...
echo.

set PYTHONPATH=%CACHE_DIR%\src;%PYTHONPATH%
%PY_CMD% "%CACHE_DIR%\src\chart_viewer\run_viewer.py" --ws ws://10.20.0.23:8765

echo.
echo Viewer-Prozess beendet (Exit-Code: %ERRORLEVEL%).
pause
