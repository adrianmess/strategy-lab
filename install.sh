#!/bin/bash

# Exit on error
set -e

echo "Installing MEXC Playwright Webhook Client and Server..."

# Check if Python 3.8+ is installed
python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if (( $(echo "$python_version < 3.8" | bc -l) )); then
    echo "Error: Python 3.8 or higher is required. You have Python $python_version"
    exit 1
fi

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Install the package in development mode
echo "Installing package in development mode..."
pip install -e .

# Install Playwright browsers
echo "Installing Playwright browsers..."
playwright install

# Create necessary directories
echo "Creating necessary directories..."
mkdir -p cache
mkdir -p chrome_user_data

# Check if uBlock Origin extension exists
if [ ! -d "ublock-origin-built" ]; then
    echo "Warning: uBlock Origin extension directory not found."
    echo "Please download and extract the uBlock Origin extension to the 'ublock-origin-built' directory."
    echo "You can find the extension at: https://github.com/gorhill/uBlock"
fi

echo "Installation complete!"
echo "To start the server, run: python webhook_server.py"
echo "To use the client, run: python webhook_client.py [command]"
