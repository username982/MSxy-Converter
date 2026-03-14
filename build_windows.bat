@echo off
:: build_windows.bat
:: -----------------
:: Builds MSxy_converter.exe on Windows using PyInstaller.
:: Run this from the MSxy_converter repo root folder.
::
:: Prerequisites (run once):
::   pip install pyinstaller
::   pip install sacn numpy scipy psutil
::
:: Output:
::   dist\MSxy_converter\MSxy_converter.exe   (and supporting files)
::
:: To distribute: zip the entire dist\MSxy_converter\ folder.
:: Users extract and double-click MSxy_converter.exe — no Python needed.

setlocal

echo.
echo ============================================================
echo  MSxy Converter -- Windows build
echo ============================================================
echo.

:: Check PyInstaller is available
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller not found.
    echo Run:  pip install pyinstaller
    pause
    exit /b 1
)

:: Clean previous build
echo Cleaning previous build...
if exist build\   rmdir /s /q build
if exist dist\    rmdir /s /q dist

:: Run PyInstaller
echo Running PyInstaller...
pyinstaller MSxy_converter.spec --noconfirm
if errorlevel 1 (
    echo.
    echo BUILD FAILED -- see errors above.
    pause
    exit /b 1
)

:: Verify the exe exists
if not exist "dist\MSxy_converter\MSxy_converter.exe" (
    echo.
    echo BUILD FAILED -- exe not found in dist\MSxy_converter\
    pause
    exit /b 1
)

:: Copy example config next to the exe so users have a starting point
copy /y led_config_example.json "dist\MSxy_converter\led_config_example.json" >nul

echo.
echo ============================================================
echo  BUILD SUCCEEDED
echo  Executable: dist\MSxy_converter\MSxy_converter.exe
echo.
echo  To distribute:
echo    zip the entire dist\MSxy_converter\ folder and share it.
echo    Users extract anywhere and double-click MSxy_converter.exe
echo    A led_config.json will be created on first run.
echo ============================================================
echo.
pause
