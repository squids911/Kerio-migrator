@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title IMAP Migrator - Build
set "APP_NAME=IMAP_Migrator"
set "SCRIPT=imap_migrator.py"
set "APP_VERSION=v1.1.50"
set "PYTHON="

color 0F

echo ========================================================
echo   Building %APP_NAME% (%APP_VERSION%)
echo ========================================================
echo.

rem Prefer the official Python Launcher, then fall back to python.exe.
where py >nul 2>&1
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON=python"
)

if not defined PYTHON (
    echo [ERROR] Python was not found.
    echo Install Python from https://www.python.org/downloads/
    echo Enable "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [INFO] Python command: %PYTHON%
%PYTHON% --version
if errorlevel 1 (
    echo [ERROR] Python cannot be started.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] File not found: %SCRIPT%
    echo Put this BAT file next to %SCRIPT% and run it again.
    pause
    exit /b 1
)

rem Make sure pip is available. This is needed on some minimal Python installs.
%PYTHON% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] pip is missing. Enabling pip...
    %PYTHON% -m ensurepip --upgrade
    if errorlevel 1 (
        echo [ERROR] Could not enable pip.
        pause
        exit /b 1
    )
)

echo.
echo [INFO] Installing PyInstaller for the current user...
%PYTHON% -m pip install --user --upgrade pyinstaller
if errorlevel 1 (
    echo [ERROR] PyInstaller installation failed.
    echo Check Internet access, proxy settings, or run this BAT as administrator.
    pause
    exit /b 1
)

if exist build (
    echo [INFO] Removing old build directory...
    rmdir /s /q build
)
if exist dist (
    echo [INFO] Removing old dist directory...
    rmdir /s /q dist
)
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo.
echo [INFO] Building standalone windowed EXE...
%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed --name "%APP_NAME%" "%SCRIPT%"
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed.
    echo To see runtime errors, build a console version with:
    echo %PYTHON% -m PyInstaller --clean --onefile --console --name "%APP_NAME%_debug" "%SCRIPT%"
    pause
    exit /b 1
)

if not exist "dist\%APP_NAME%.exe" (
    echo [ERROR] PyInstaller finished, but the EXE was not found.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   Build complete!
echo   EXE: %CD%\dist\%APP_NAME%.exe
echo ========================================================
pause
endlocal
