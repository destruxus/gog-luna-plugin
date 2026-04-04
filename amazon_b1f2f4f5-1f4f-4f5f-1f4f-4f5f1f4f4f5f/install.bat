@echo off
setlocal enabledelayedexpansion

echo Installing Amazon Luna plugin...

set PLUGIN_DIR=%CD%
set TARGET=%LOCALAPPDATA%\GOG.com\Galaxy\plugins\installed\amazon_b1f2f4f5-1f4f-4f5f-1f4f-4f5f1f4f4f5f

if not exist "!PLUGIN_DIR!\plugin.py" (
    echo ERROR: plugin.py not found. Run install.bat from inside the plugin folder.
    pause
    exit /b 1
)

taskkill /F /IM GalaxyClient.exe >nul 2>&1
timeout /t 2 >nul

if exist "!TARGET!" rmdir /S /Q "!TARGET!"
xcopy /E /I /Y "!PLUGIN_DIR!" "!TARGET!" >nul

echo Installation complete!
echo.
echo Start GOG Galaxy now? (Y/N)
set /p START=
if /i "!START!"=="Y" start "" "%ProgramFiles(x86)%\GOG Galaxy\GalaxyClient.exe"
