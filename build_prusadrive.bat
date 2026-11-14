@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

echo ==============================================
echo          PrusaDrive Build Utility
echo ==============================================
echo.

REM Select the highest numeric version using the installed Python 3.11.
REM This handles v020, v021, v100, etc., regardless of filename sorting.
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

REM Use Python 3.11 consistently for dependency checks and compilation.
py -3.11 -c "import _cffi_backend, winfspy, requests, pystray, PIL, PyInstaller" 
if errorlevel 1 (
    echo.
    echo ERROR: Missing Python 3.11 or required packages.
    echo Install using:
    echo py -3.11 -m pip install pyinstaller cffi winfspy requests pystray pillow
    goto :failed
)

REM Build to a staging folder so an existing working EXE stays untouched
REM if compilation fails or the old EXE is currently running.
py -3.11 -m PyInstaller --noconfirm --clean --onefile --windowed --name PrusaDrive --distpath "dist" --workpath "build" --specpath "." --hidden-import=_cffi_backend --collect-all winfspy --collect-all pystray --collect-all PIL "%LATEST%"
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
