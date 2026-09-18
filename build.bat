@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  Pathogene - Windows Build
echo ============================================================
echo.

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

echo [1/7] Project path: %PROJECT_DIR%
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ from https://www.python.org
    pause & exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [INFO] System Python: %PY_VER%
echo.

set "VENV_DIR=%PROJECT_DIR%\.venv-build"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [2/7] Reusing existing venv: %VENV_DIR%
) else (
    echo [2/7] Creating venv: %VENV_DIR%
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        pause & exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"
echo [INFO] venv activated
echo.

echo [INFO] Build venv Python:
where python
python -c "import sys; print(sys.executable)"
echo.

echo [3/7] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo.

echo [4/7] Installing dependencies...
python -m pip install -r "%PROJECT_DIR%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.txt
    pause & exit /b 1
)

echo.
echo [CHECK] Verifying pyvips in build venv...
python -m pip show pyvips >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pyvips is NOT installed in .venv-build
    echo         Run: %VENV_DIR%\Scripts\python.exe -m pip install -r requirements.txt
    pause & exit /b 1
)

python -c "import pyvips; print('pyvips OK before PyInstaller:', pyvips.__version__)"
if errorlevel 1 (
    echo [ERROR] pyvips import failed inside .venv-build
    pause & exit /b 1
)

echo.
echo [INFO] Skipping manual Tcl/Tk detection.
echo        PyInstaller will handle bundled Tcl/Tk files automatically.
echo.

echo [INFO] Installing openslide-bin (Windows DLLs)...
python -m pip install openslide-bin
if errorlevel 1 (
    echo [WARN] openslide-bin install failed - SVS files may not open
)
echo.

python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    python -m pip install pyinstaller
)
echo.

echo [5/7] Cleaning previous build...
if exist "%PROJECT_DIR%\dist\Pathogene" rmdir /s /q "%PROJECT_DIR%\dist\Pathogene"
if exist "%PROJECT_DIR%\build\Pathogene" rmdir /s /q "%PROJECT_DIR%\build\Pathogene"
echo.

echo [INFO] Setting PATHOGENE_PYTHON fallback for subprocess scripts...
set "PATHOGENE_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo [6/7] Building with PyInstaller...
echo.

cd /d "%PROJECT_DIR%"
python -m PyInstaller pathogene.spec --noconfirm --clean

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed!
    pause & exit /b 1
)

set "DIST_DIR=%PROJECT_DIR%\dist\Pathogene"
set "DIST_EXE=%DIST_DIR%\Pathogene.exe"

if not exist "%DIST_DIR%" (
    echo [ERROR] Dist folder was not created: %DIST_DIR%
    pause & exit /b 1
)

echo.
echo [CHECK] Verifying pyvips files in dist...
dir "%DIST_DIR%\_internal" | findstr /i "pyvips vips" || echo [WARN] pyvips may be inside PYZ archive; run-time test will confirm.

for %%d in (slides tiles cosmx cosmx_tiles annotations qc_results uploads) do (
    if not exist "%DIST_DIR%\data\%%d" mkdir "%DIST_DIR%\data\%%d"
)

echo.
echo [7/7] Creating desktop shortcut...

set "SHORTCUT_PATH=%USERPROFILE%\Desktop\Pathogene.lnk"
set "PS1_TMP=%TEMP%\make_shortcut.ps1"

(
    echo $ws = New-Object -ComObject WScript.Shell
    echo $sc = $ws.CreateShortcut('%SHORTCUT_PATH%'^)
    echo $sc.TargetPath = '%DIST_EXE%'
    echo $sc.WorkingDirectory = '%DIST_DIR%'
    echo $sc.Description = 'Pathogene'
    echo $sc.Save(^)
) > "%PS1_TMP%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_TMP%"
del "%PS1_TMP%" >nul 2>&1

if exist "%SHORTCUT_PATH%" (
    echo [INFO] Desktop shortcut created: %SHORTCUT_PATH%
) else (
    echo [WARN] Could not create desktop shortcut. Create it manually.
)

echo.
echo ============================================================
echo  Build complete!
echo ============================================================
echo.
echo  Executable : %DIST_EXE%
echo  Dist folder: %DIST_DIR%
echo  Shortcut   : %SHORTCUT_PATH%
echo.
echo  To distribute: zip the entire dist\Pathogene\ folder.
echo ============================================================
echo.

pause
