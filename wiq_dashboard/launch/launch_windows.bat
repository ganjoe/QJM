@echo off
setlocal enabledelayedexpansion
title WIQ Dashboard (Windows)

REM ---------------------------------------------------------------------------
REM  Start aus einem UNC-Pfad abfangen.
REM
REM  Wird diese Datei per \\server\share\... gestartet, kann cmd.exe nicht
REM  arbeiten ("UNC-Pfade werden nicht als aktuelles Verzeichnis unterstuetzt")
REM  und Windows meldet einen Zugriffsfehler. Deshalb kopiert sich das Skript
REM  in diesem Fall nach %TEMP% und laeuft von dort weiter. Ueber ein gemapptes
REM  Laufwerk (Z:\...) oder lokal startet es direkt.
REM ---------------------------------------------------------------------------
set "SELFDIR=%~dp0"
if "%SELFDIR:~0,2%"=="\\" goto from_network
goto local_start

:from_network
echo Start aus dem Netzwerkordner erkannt.
echo Kopiere das Skript nach %TEMP% und starte von dort...
copy /y "%~f0" "%TEMP%\wiq_dashboard_launch.bat" >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Kopie nach %TEMP% fehlgeschlagen.
    pause & exit /b 1
)
start "WIQ Dashboard" "%TEMP%\wiq_dashboard_launch.bat"
exit /b 0

:local_start

REM ============================================================
REM  WIQ Dashboard - Client fuer Windows
REM
REM  Holt den Client-Code vom QJM-Server, richtet einmalig eine
REM  eigene venv mit PySide6 ein und startet das Fenster.
REM  Muster: chart_viewer\launch_windows_v2.bat
REM
REM  Schalter:
REM    SERVER_HOST=...   anderer Server
REM    FORCE_SYNC=1      Code neu laden, auch wenn die Version passt
REM    RESET_VENV=1      venv neu aufbauen
REM ============================================================

if not defined SERVER_HOST set "SERVER_HOST=10.20.0.23"
set "SERVER=http://%SERVER_HOST%:8799"
set "CACHE_DIR=%LOCALAPPDATA%\WIQDashboard"
set "VENV_DIR=%CACHE_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VERSION_FILE=%CACHE_DIR%\sync_version"

echo ========================================================
echo             WIQ Dashboard  (Server: %SERVER%)
echo ========================================================
echo.

if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" >nul 2>&1
if not exist "%CACHE_DIR%" (
    echo FEHLER: Verzeichnis nicht beschreibbar: %CACHE_DIR%
    pause & exit /b 1
)

REM --- 1/4 Server erreichbar? -------------------------------------------
echo [1/4] Pruefe Server...
curl -s -f -m 8 "%SERVER%/health" -o "%CACHE_DIR%\health.json" >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Server nicht erreichbar unter %SERVER%
    echo        Laeuft der Container qjm-wiq-dashboard auf %SERVER_HOST%?
    pause & exit /b 1
)
echo       [OK]

REM --- 2/4 Code abgleichen (nur bei neuer Version) ----------------------
set "SYNC_NEEDED=0"
if defined FORCE_SYNC set "SYNC_NEEDED=1"
set "NEW_VER="
curl -s -f -m 8 "%SERVER%/api/sync_version" -o "%CACHE_DIR%\sync_version.new" >nul 2>&1
if not errorlevel 1 (
    set /p NEW_VER=<"%CACHE_DIR%\sync_version.new"
    del "%CACHE_DIR%\sync_version.new" >nul 2>&1
)
set "OLD_VER="
if exist "%VERSION_FILE%" set /p OLD_VER=<"%VERSION_FILE%"
if not "%NEW_VER%"=="%OLD_VER%" set "SYNC_NEEDED=1"

if "%SYNC_NEEDED%"=="1" (
    echo [2/4] Lade Client-Code vom Server...
    curl -s -f -m 30 "%SERVER%/api/sync" -o "%CACHE_DIR%\client.tar"
    if errorlevel 1 (
        echo FEHLER: Code konnte nicht geladen werden.
        pause & exit /b 1
    )
    tar -xf "%CACHE_DIR%\client.tar" -C "%CACHE_DIR%"
    if errorlevel 1 (
        echo FEHLER: Entpacken fehlgeschlagen.
        pause & exit /b 1
    )
    del "%CACHE_DIR%\client.tar" >nul 2>&1
    >"%VERSION_FILE%" echo %NEW_VER%
    REM !...! statt %...%: in einem Block wertet cmd sonst zum Parse-Zeitpunkt aus.
    echo       [OK] Stand !NEW_VER:~0,12!
) else (
    echo [2/4] Code ist aktuell.
)

REM --- 3/4 Python und PySide6 -------------------------------------------
echo [3/4] Pruefe Python-Umgebung...
if exist "%VENV_PY%" if not defined RESET_VENV goto venv_ok

set "BASE_PY="
where py >nul 2>&1 && set "BASE_PY=py -3"
if not defined BASE_PY where python >nul 2>&1 && set "BASE_PY=python"
if not defined BASE_PY (
    echo FEHLER: Kein Python gefunden. Bitte Python 3.10-3.13 installieren:
    echo         https://www.python.org/downloads/windows/
    pause & exit /b 1
)

if defined RESET_VENV if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
echo       Erstelle venv (einmalig)...
%BASE_PY% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo FEHLER: venv konnte nicht erstellt werden.
    pause & exit /b 1
)
"%VENV_PY%" -m pip install --quiet --upgrade pip
"%VENV_PY%" -m pip install --quiet "PySide6>=6.5.0"
if errorlevel 1 (
    echo FEHLER: PySide6 konnte nicht installiert werden.
    pause & exit /b 1
)

:venv_ok
echo       [OK]

REM --- 4/4 Starten -------------------------------------------------------
echo [4/4] Starte Dashboard...
echo.
"%VENV_PY%" "%CACHE_DIR%\run_dashboard.py" --url "%SERVER%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Client beendet (Exit-Code %EXIT_CODE%).
pause
endlocal
