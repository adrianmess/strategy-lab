# Project Structure

This document provides a detailed explanation of the files and directories in the MEXC Trading Bot project.

## Core Files

### `open_browser.py`

This script opens a Chromium browser with the uBlock Origin extension installed and navigates to the MEXC website. It uses a persistent browser profile to remember settings between sessions.

**Key features:**
- Loads the uBlock Origin extension from the `ublock-origin-built` directory
- Uses a persistent Chrome user data directory to remember settings
- Navigates to the MEXC futures trading page
- Takes a screenshot of the page for verification
- Waits for user input before closing the browser

### `mexc_with_proxy.py`

This script is similar to `open_browser.py` but includes additional functionality to check if the user is logged in to MEXC.

**Key features:**
- Loads the uBlock Origin extension from the `ublock-origin-built` directory
- Uses a persistent Chrome user data directory to remember settings
- Navigates to the MEXC futures trading page
- Checks if the user is logged in by looking for the "Wallets" element
- Takes a screenshot of the page for verification
- Waits for a specified time before closing the browser

## Extension Directory

### `ublock-origin-built/`

This directory contains the built uBlock Origin extension. The extension is loaded by the scripts to block ads and trackers on the MEXC website.

**Key files:**
- `manifest.json`: The extension manifest file that defines the extension's properties and permissions
- Various HTML, CSS, and JavaScript files that make up the extension

## Configuration Files

### `.gitignore`

This file specifies which files and directories should be ignored by Git. It excludes:
- Python cache files and virtual environments
- IDE-specific files
- Playwright-specific files
- Chrome user data directory
- Screenshots and logs

### `README.md`

This file provides an overview of the project, including:
- Project description
- Features
- Requirements
- Installation instructions
- Usage instructions
- License information

## Generated Files

### `chrome_user_data/`

This directory is created by the scripts to store Chrome user data. It allows the browser to remember settings between sessions, including uBlock Origin settings.

### Screenshots

The scripts generate screenshots of the MEXC website:
- `mexc_homepage.png`: A screenshot of the MEXC homepage
- `mexc_timeout.png`: A screenshot taken if there's a timeout when accessing MEXC

## How the Files Work Together

1. The scripts load the uBlock Origin extension from the `ublock-origin-built` directory
2. They use a persistent Chrome user data directory to remember settings
3. They navigate to the MEXC website and perform various actions
4. They take screenshots for verification
5. They wait for user input or a specified time before closing the browser

The project is designed to automate interactions with the MEXC cryptocurrency exchange using Playwright, with the uBlock Origin extension providing ad blocking functionality.