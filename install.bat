@echo off
echo Installing MEXC Playwright Webhook Client and Server...

REM Check if Python 3.8+ is installed
python --version > nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Error: Python is not installed or not in PATH
    exit /b 1
)

REM Create virtual environment
echo Creating virtual environment...
python -m venv venv

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

REM Install the package in development mode
echo Installing package in development mode...
pip install -e .

REM Install Playwright browsers
echo Installing Playwright browsers...
playwright install

REM Create necessary directories
echo Creating necessary directories...
if not exist cache mkdir cache
if not exist chrome_user_data mkdir chrome_user_data

REM Check if uBlock Origin extension exists
if not exist ublock-origin-built (
    echo Warning: uBlock Origin extension directory not found.
    echo Please download and extract the uBlock Origin extension to the 'ublock-origin-built' directory.
    echo You can find the extension at: https://github.com/gorhill/uBlock
)

echo Installation complete!
echo To start the server, run: python webhook_server.py
echo To use the client, run: python webhook_client.py [command]