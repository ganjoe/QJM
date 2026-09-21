@echo off
setlocal enabledelayedexpansion
title Chart Viewer Client Windows (v2)

REM ============================================================
REM  Chart Viewer - Windows Bootstrap v2 (diagnosefaehig)
REM
REM  Unterschiede zu v1:
REM   [A] Microsoft-Store-Python-Alias wird erkannt und verworfen
REM   [B] Versionspruefung des Interpreters (empfohlen 3.10 - 3.13)
REM   [C] isolierte venv unter %LOCALAPPDATA%\ChartViewer\venv
REM       (keine Adminrechte, kein Konflikt mit System-Python)
REM   [D] pip-Ausgabe wird geloggt und im Fehlerfall angezeigt
REM   [E] PyPI-Erreichbarkeit + pip-Konfiguration werden geprueft
REM   [F] automatische Python-Installation wenn keines vorhanden ist:
REM       winget -> Download von python.org -> lokaler Installer neben der .bat
REM
REM  Schalter:
REM   USE_VENV=1 -> isolierte venv (Default)
REM   USE_VENV=0 -> System-Python mit --user
REM ============================================================

set "SERVER_HOST=10.20.0.23"
set "SERVER_HTTP=http://%SERVER_HOST%:8766"
set "SERVER_WS=ws://%SERVER_HOST%:8765"
set "USE_VENV=1"

echo ========================================================
echo       TC2000-Style Desktop Chart Viewer Client
echo ========================================================
echo.

set "CACHE_DIR=%LOCALAPPDATA%\ChartViewer"
set "LOG=%CACHE_DIR%\install.log"
set "VENV_DIR=%CACHE_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" >nul 2>&1
if not exist "%CACHE_DIR%" (
    echo FEHLER: Cache-Verzeichnis nicht beschreibbar: %CACHE_DIR%
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM  STUFE 1: Python-Interpreter finden
REM ------------------------------------------------------------
set "ATTEMPT=1"
:detect
echo [1/4] Suche Python-Interpreter...

REM Store-Platzhalter vorab kenntlich machen (haeufigste Ursache)
where python >"%CACHE_DIR%\where_python.txt" 2>nul
findstr /i "WindowsApps" "%CACHE_DIR%\where_python.txt" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [HINWEIS] "python" verweist auf den Microsoft-Store-Platzhalter:
    type "%CACHE_DIR%\where_python.txt"
    echo           Das ist KEIN installiertes Python. Bitte von python.org installieren.
    echo.
)

set "BASE_PY="
set "PY_VER="
set "PY_EXE="

call :try_py "py -3.13"
call :try_py "py -3.12"
call :try_py "py -3.11"
call :try_py "py -3.10"
call :try_py "py -3"

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try_py "%%~D\python.exe"
for /d %%D in ("C:\Program Files\Python3*") do call :try_py "%%~D\python.exe"
for /d %%D in ("C:\Python3*") do call :try_py "%%~D\python.exe"

call :try_py "python"
call :try_py "python3"

if not defined BASE_PY goto :no_python
echo [OK] Interpreter: Python !PY_VER!  ^(!PY_EXE!^)
echo.

REM ------------------------------------------------------------
REM  STUFE 2a: Laufzeitumgebung (optional venv)
REM ------------------------------------------------------------
set "PY_CMD=%BASE_PY%"

if "%USE_VENV%"=="1" (
    if not exist "%VENV_PY%" (
        echo [2/4] Erstelle isolierte Umgebung: %VENV_DIR%
        %BASE_PY% -m venv "%VENV_DIR%" >"%LOG%" 2>&1
        if exist "%VENV_PY%" (
            echo [OK] venv erstellt.
        ) else (
            echo [WARN] venv-Erstellung fehlgeschlagen - nutze System-Python.
            type "%LOG%"
        )
    )
)

if exist "%VENV_PY%" set "PY_CMD="%VENV_PY%""

REM ------------------------------------------------------------
REM  STUFE 2b: Server-Sync (Logik identisch zu v1)
REM ------------------------------------------------------------
echo [2/4] Pruefe Server-Version (%SERVER_HTTP%)...
set "SYNC_NEEDED=1"
set "NEW_VER="

curl.exe -s -f -m 5 "%SERVER_HTTP%/api/sync_version" -o "%CACHE_DIR%\sync_version.new" >nul 2>&1
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
    echo Lade neueste Version vom Server...
    curl.exe -s -f -m 20 "%SERVER_HTTP%/api/sync" -o "%CACHE_DIR%\src_bundle.tar"
    if not errorlevel 1 (
        tar.exe -xf "%CACHE_DIR%\src_bundle.tar" -C "%CACHE_DIR%"
        del /f /q "%CACHE_DIR%\src_bundle.tar" >nul 2>&1
        if defined NEW_VER (
            >"%CACHE_DIR%\sync_version" echo !NEW_VER!
        )
        echo [OK] Code erfolgreich auf den neuesten Stand aktualisiert!
    ) else (
        del /f /q "%CACHE_DIR%\src_bundle.tar" >nul 2>&1
        echo [INFO] Server-Sync uebersprungen - Server offline oder nicht erreichbar.
    )
)
echo.

REM ------------------------------------------------------------
REM  STUFE 3: Abhaengigkeiten
REM ------------------------------------------------------------
echo [3/4] Pruefe Abhaengigkeiten (PySide6, msgspec, websockets)...
%PY_CMD% -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','msgspec','websockets')) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_app

echo.
echo Installiere erforderliche Pakete (PySide6, msgspec, websockets)...
echo Vollstaendiges Log: %LOG%
echo.
del /f /q "%LOG%" >nul 2>&1

echo === Interpreter === >>"%LOG%"
%PY_CMD% -c "import sys; print(sys.version); print(sys.executable)" >>"%LOG%" 2>&1
echo === pip === >>"%LOG%"
%PY_CMD% -m pip --version >>"%LOG%" 2>&1
%PY_CMD% -m pip config list >>"%LOG%" 2>&1
echo === Env === >>"%LOG%"
echo HTTP_PROXY=%HTTP_PROXY% >>"%LOG%"
echo HTTPS_PROXY=%HTTPS_PROXY% >>"%LOG%"
echo === PyPI-Erreichbarkeit === >>"%LOG%"
curl.exe -s -f -m 8 -o nul https://pypi.org/simple/pyside6/ >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [WARN] pypi.org nicht erreichbar - Firewall, Proxy oder Offline?
) else (
    echo [OK] pypi.org erreichbar.
)
echo === pip install === >>"%LOG%"

%PY_CMD% -m pip install --no-input --disable-pip-version-check --upgrade pip >>"%LOG%" 2>&1
%PY_CMD% -m pip install --no-input --disable-pip-version-check PySide6 msgspec websockets >>"%LOG%" 2>&1
if not errorlevel 1 goto :deps_installed

REM --user ist innerhalb einer venv unzulaessig -> nur ohne venv versuchen
if exist "%VENV_PY%" goto :install_failed
echo [WARN] Erster Versuch fehlgeschlagen - versuche --user...
%PY_CMD% -m pip install --no-input --disable-pip-version-check --user PySide6 msgspec websockets >>"%LOG%" 2>&1
if not errorlevel 1 goto :deps_installed
goto :install_failed

:deps_installed
%PY_CMD% -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','msgspec','websockets')) else 1)" >nul 2>&1
if errorlevel 1 goto :install_failed
echo [OK] Abhaengigkeiten installiert.
echo.

REM ------------------------------------------------------------
REM  STUFE 4: Viewer starten
REM ------------------------------------------------------------
:run_app
if not exist "%CACHE_DIR%\src\chart_viewer\run_viewer.py" (
    echo ========================================================
    echo FEHLER: Client-Code fehlt.
    echo ========================================================
    echo Erwartet: %CACHE_DIR%\src\chart_viewer\run_viewer.py
    echo Der Server-Sync ist fehlgeschlagen. Server erreichbar? %SERVER_HTTP%
    echo.
    pause
    exit /b 1
)

echo [4/4] Starte Desktop Chart Viewer Client...
echo Verbinde mit %SERVER_WS%...
echo.

set "PYTHONPATH=%CACHE_DIR%\src;%PYTHONPATH%"
%PY_CMD% "%CACHE_DIR%\src\chart_viewer\run_viewer.py" --ws %SERVER_WS%

echo.
echo Viewer-Prozess beendet (Exit-Code: %ERRORLEVEL%).
pause
exit /b 0

REM ============================================================
REM  Fehlerpfade
REM ============================================================
:install_failed
echo.
echo ========================================================
echo FEHLER: Installation der Abhaengigkeiten fehlgeschlagen.
echo ========================================================
echo.
echo --- Log: %LOG% ---
type "%LOG%"
echo --- Ende Log ---
echo.
echo Moegliche Ursachen:
echo   1^) Python-Version zu neu (3.14+) - PySide6 liefert dafuer oft noch
echo      keine Wheels. FIX: Python 3.13.x installieren
echo      https://www.python.org/ftp/python/3.13.8/python-3.13.8-amd64.exe
echo   2^) Kein echtes Python, nur der Microsoft-Store-Alias.
echo   3^) pypi.org nicht erreichbar (Firewall / Proxy / TLS-Inspektion).
echo      Proxy setzen:  set HTTPS_PROXY=http://user:pass@proxy:port
echo   4^) pip defekt:  %PY_CMD% -m ensurepip --upgrade
echo.
pause
exit /b 1

:no_python
echo ========================================================
echo FEHLER: Kein nutzbares Python 3 gefunden.
echo ========================================================
echo.
echo Hinweis: Der Microsoft-Store-Alias "python" zaehlt NICHT als Installation.
echo.

if not "%ATTEMPT%"=="1" goto :no_python_manual

REM --- Weg 1: winget (falls der App Installer vorhanden ist) ---
where winget >nul 2>&1
if errorlevel 1 goto :no_python_offline

set "DO_INSTALL="
echo Python kann automatisch installiert werden (via winget).
echo    Installationsziel: %LOCALAPPDATA%\Programs\Python\Python313
set /p "DO_INSTALL=  Jetzt installieren? [j/n]: "
if /i not "!DO_INSTALL!"=="j" goto :no_python_offline

set "ATTEMPT=2"
echo.
echo Installiere Python 3.13 - das dauert ein bis zwei Minuten...
echo.
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
echo.
echo Pruefe erneut...
echo.
goto :detect

REM --- Weg 2: lokaler Installer neben dieser .bat ODER Download von python.org ---
:no_python_offline
set "ARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=arm64"

set "INSTALLER="
for %%F in ("%~dp0python-*.exe") do if exist "%%~fF" set "INSTALLER=%%~fF"
if defined INSTALLER (
    echo [OK] Lokaler Installer neben diesem Skript gefunden:
    echo      !INSTALLER!
    if "!INSTALLER:~0,2!"=="\\" (
        echo [INFO] Installer liegt auf einer Freigabe - kopiere ihn zuerst lokal.
        copy /y "!INSTALLER!" "%TEMP%\python-setup.exe" >nul 2>&1
        if exist "%TEMP%\python-setup.exe" (
            set "INSTALLER=%TEMP%\python-setup.exe"
            echo      Ziel: %TEMP%\python-setup.exe
        ) else (
            echo [WARN] Kopieren fehlgeschlagen - starte direkt von der Freigabe.
        )
    )
    goto :have_installer
)

echo Python wird direkt von python.org geladen (offizielle Quelle, ~28 MB).
echo    Zieldatei: %TEMP%\python-setup.exe
set "DO_INSTALL="
set /p "DO_INSTALL=  Jetzt herunterladen und installieren? [j/n]: "
if /i not "%DO_INSTALL%"=="j" goto :no_python_manual

set "INSTALLER=%TEMP%\python-setup.exe"
set "DL_OK="
for %%U in (https://www.python.org/ftp/python/3.13.8/python-3.13.8-%ARCH%.exe https://www.python.org/ftp/python/3.12.10/python-3.12.10-%ARCH%.exe) do (
    if not defined DL_OK (
        echo   Lade %%U
        del /f /q "%INSTALLER%" >nul 2>&1
        curl.exe -s -L -f -m 600 -o "%INSTALLER%" "%%U"
        if not errorlevel 1 if exist "%INSTALLER%" set "DL_OK=1"
    )
)
if not defined DL_OK (
    echo.
    echo [WARN] Download fehlgeschlagen - kein Internetzugang zu python.org?
    goto :no_python_manual
)

:have_installer
echo.
echo Installiere Python (nur aktueller Benutzer, ohne Adminrechte)...
echo.
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 Include_doc=0 Include_tcltk=0
if errorlevel 1 (
    echo [WARN] Der Installer meldete Exitcode !ERRORLEVEL! - moeglicherweise
    echo        blockiert eine Richtlinie ^(AppLocker/SRP^) die Installation.
)
set "ATTEMPT=2"
echo.
echo Pruefe erneut...
echo.
goto :detect

:no_python_manual
echo Bitte Python manuell installieren:
echo    https://www.python.org/downloads/
echo    Empfohlen:  Python 3.13.x   ^(3.14 meiden - PySide6-Wheels fehlen ggf.^)
echo    Direktlink: https://www.python.org/ftp/python/3.13.8/python-3.13.8-amd64.exe
echo    Installation:  ^[x^] Add python.exe to PATH
echo.
echo    Hinweis: 3.12.11 und neuer haben KEINE Windows-Installer mehr,
echo             letzter installierbarer 3.12-Stand ist 3.12.10.
echo.
echo Offline-Client ohne Internet: die Datei python-3.13.8-amd64.exe einfach
echo neben dieses Skript legen - dann wird sie automatisch verwendet.
echo.
pause
exit /b 1

REM ============================================================
REM  Hilfsroutine: Kandidat pruefen und ggf. als BASE_PY setzen
REM ============================================================
:try_py
if defined BASE_PY goto :eof

set "CAND=%~1"
if "%CAND%"=="" goto :eof

REM Pfade mit Leerzeichen in Anfuehrungszeichen setzen ("py -3.13" bleibt unquoted)
set "RUN=%CAND%"
if not "%CAND%"=="%CAND:\=%" set "RUN="%CAND%""

REM Lauffaehigkeit pruefen (der Store-Stub faellt hier bereits durch)
%RUN% -c "import sys" >nul 2>&1
if errorlevel 1 goto :eof

REM Store-Alias ausschliessen
%RUN% -c "import sys; sys.exit(1 if 'WindowsApps' in (sys.executable or '') else 0)" >nul 2>&1
if errorlevel 1 (
    echo [SKIP] %CAND%  -^> Microsoft-Store-Platzhalter
    goto :eof
)

set "CVER="
set "CEXE="
for /f "delims=" %%V in ('%RUN% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "CVER=%%V"
for /f "delims=" %%E in ('%RUN% -c "import sys;print(sys.executable)" 2^>nul') do set "CEXE=%%E"
if not defined CVER goto :eof

set "BASE_PY=%RUN%"
set "PY_VER=%CVER%"
set "PY_EXE=%CEXE%"

set "PY_NUM="
for /f "tokens=1,2 delims=." %%A in ("%CVER%") do set "PY_NUM=%%A%%B"
if defined PY_NUM (
    if %PY_NUM% GTR 313 (
        echo [WARN] Python %CVER% ist sehr neu - fuer PySide6 gibt es evtl. noch keine Wheels.
        echo        Empfehlung: Python 3.12.x oder 3.13.x
    )
    if %PY_NUM% LSS 310 (
        echo [WARN] Python %CVER% ist zu alt - der Viewer verlangt Python ^>= 3.10.
    )
)
goto :eof
