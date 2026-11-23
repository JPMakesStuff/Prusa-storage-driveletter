@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

echo ==============================================
echo          PrusaDrive Build Utility
echo ==============================================
echo.

REM Select the highest numeric version using Python 3.11.
set "LATEST="
for /f "usebackq delims=" %%F in (`py -3.11 -c "import pathlib,re; files=[(int(m.group(1)),p.name) for p in pathlib.Path('.').iterdir() if p.is_file() if (m:=re.fullmatch(r'prusa_drive_v([0-9]+)\.py',p.name,re.I))]; print(max(files)[1] if files else '')"`) do set "LATEST=%%F"

if not defined LATEST (
    echo ERROR: No matching prusa_drive_vNNN.py source file found.
    echo Place this batch file alongside the versioned Python files.
    goto :failed
)

echo Selected source: %LATEST%
echo Output: %CD%\PrusaDrive.exe
echo.

if not exist "requirements.txt" (
    echo ERROR: requirements.txt was not found in %CD%
    goto :failed
)

REM Install or verify dependencies from one maintained requirements file.
echo Checking Python dependencies from requirements.txt...
py -3.11 -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    goto :failed
)

REM Verify required modules before building.
py -3.11 -c "import _cffi_backend, winfspy, requests, pystray, PIL, PyInstaller"
if errorlevel 1 (
    echo ERROR: Python 3.11 or required packages are unavailable.
    goto :failed
)

REM Use the icon for the EXE and bundle it for the tray icon code.
if not exist "PrusaDrive.ico" (
    echo ERROR: PrusaDrive.ico was not found in %CD%
    goto :failed
)

REM Build to staging so the existing EXE remains untouched if the build fails.
py -3.11 -m PyInstaller --noconfirm --clean --onefile --windowed --name PrusaDrive --distpath "dist" --workpath "build" --specpath "." --hidden-import=_cffi_backend --collect-all winfspy --collect-all pystray --collect-all PIL --icon "PrusaDrive.ico" --add-data "PrusaDrive.ico;." "%LATEST%"
if errorlevel 1 (
    echo ERROR: PyInstaller build failed. Existing PrusaDrive.exe was not replaced.
    goto :failed
)

if not exist "dist\PrusaDrive.exe" (
    echo ERROR: PyInstaller did not create dist\PrusaDrive.exe.
    goto :failed
)

copy /y "dist\PrusaDrive.exe" "PrusaDrive.exe" >nul
if errorlevel 1 (
    echo ERROR: Could not replace PrusaDrive.exe.
    echo Exit the running PrusaDrive from the system tray and try again.
    echo The new build remains available in dist\PrusaDrive.exe.
    goto :failed
)

echo.
echo SUCCESS: Built PrusaDrive.exe from %LATEST%
echo Keep printers.json alongside PrusaDrive.exe.
echo.
pause
exit /b 0

:failed
echo.
echo BUILD NOT COMPLETED.
pause
exit /b 1
