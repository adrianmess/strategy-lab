# MEXC Trading Bot with Playwright

This project implements a trading bot for MEXC using Playwright for browser automation. It includes a webhook server that receives trading signals from TradingView and executes trades on MEXC.

## Features

- Automated trading based on TradingView webhook signals
- Browser automation using Playwright with `force=True` clicks to bypass UI overlays
- Reliable Playwright-only execution for all trade actions
- Automatic dismissal of MEXC feature popovers and guide modals before every action
- Manual login in a persistent Chrome profile — no credentials or captcha-solver code in the project
- Support for multiple independent instances
- uBlock Origin integration for ad blocking
- Optional `--debug` flag for screenshot capture and form-state diagnostics

## Prerequisites

- Python 3.8 or higher
- Chrome browser installed
- uBlock Origin extension (built and extracted to `ublock-origin-built` directory)

## Installation

1. Clone the repository

2. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
   > **Tip:** Add `.venv/` to your `.gitignore` so it is never committed.  
   > To deactivate the environment later, run `deactivate`.

3. Install Python dependencies inside the venv:
   ```bash
   pip install -r requirements.txt
   ```

4. Install Playwright browsers:
   ```bash
   python3 -m playwright install chromium
   ```

4.1 Install Cloudflared:
   ```bash
   brew install cloudflared
   cloudflared tunnel login
   ```
    
4. Set up the uBlock Origin extension:
   - Download the uBlock Origin extension from the [Chrome Web Store](https://chrome.google.com/webstore/detail/ublock-origin/cjpalhdlnbpafiamejdnhcphjbkeiagm)
   - Extract the extension files to the `ublock-origin-built` directory in the project root
   - Make sure the following files and directories exist:
     - `ublock-origin-built/manifest.json`
     - `ublock-origin-built/img/icon_16.png` (and other icon files)
     - `ublock-origin-built/js/` directory
     - `ublock-origin-built/css/` directory

5. Create necessary directories:
   ```bash
   mkdir -p cache
   mkdir -p chrome_user_data
   ```

## Configuration

1. Copy `.env.example` to `.env` and adjust the defaults if you want (symbol, leverage, instance ID, port, login wait). No credentials are stored by this project — login is always performed manually.

2. Configure TradingView webhook alerts to point to your server's IP and port.

## Logging In

This project **does not** automate login. On startup, each instance opens a Chrome window pointed at the MEXC futures page. If you are not already logged in (either because the profile is new or MEXC has expired the session), **log in manually** in that window — including any GeeTest slider, reCAPTCHA, and 2FA prompts.

- The server will wait up to `MEXC_LOGIN_WAIT_SECONDS` (default 300s / 5 minutes) for the trading interface to appear after the page loads. Override via `.env` if you need more time.
- Chrome saves your session to `chrome_user_data/instance_<N>/`, so subsequent starts skip the login prompt until MEXC expires the cookies.
- If the 5-minute window elapses before you finish logging in, the startup will fail. Just restart the server and try again.

## Running the Bot

> **Always activate the virtual environment first:**
 ```bash
 source venv/bin/activate
 ```

### Single Instance

```bash
python webhook_server.py --instance 1 --port 8001
```

### Multiple Instances

Start each instance with a different instance ID and port (each in its own terminal with the venv active):
```bash
# Instance 1
python webhook_server.py --instance 1 --port 8001

# Instance 2
python webhook_server.py --instance 2 --port 8002

# Instance 3
python webhook_server.py --instance 3 --port 8003
```

Each instance will:
- Use its own browser profile
- Maintain its own cache
- Run on its own port
- Handle its own webhook requests independently

### Debug Mode

Add the `--debug` flag to enable diagnostic features. **Do not use in production** — debug mode adds latency from screenshots and extra JS evaluation on every trade action.

```bash
python webhook_server.py --instance 1 --port 8001 --debug
```

When debug mode is active, each `open_long` / `open_short` call will:

1. Log a **form state snapshot** before any clicks, e.g.:
   ```
   Form state [open_long_start]: {'openTab': True, 'openLongBtn': True, 'sliderSpanCount': 10, 'sliderNth5': True, 'visibleOverlays': ['ant-popover-v5']}
   ```
   Key fields to watch:
   | Field | What to look for |
   |---|---|
   | `sliderSpanCount` | Should be ≥ 5; if < 5, `span:nth-child(5)` won't exist |
   | `sliderNth5` | Must be `True` for the quantity slider to be clickable |
   | `visibleOverlays` | Should be empty; any entry here means a popup survived `close_popovers` and may still block clicks |

2. Save **four timestamped PNG screenshots** to the project root:
   | File | Captured after |
   |---|---|
   | `debug_open_long_1_start_<ts>.png` | `close_popovers()` — shows overlay state before any clicks |
   | `debug_open_long_2_after_tabs_<ts>.png` | Open + Market tab clicks |
   | `debug_open_long_3_after_slider_<ts>.png` | Quantity slider click |
   | `debug_open_long_4_after_button_<ts>.png` | Open Long button click |
   | `debug_open_long_error_<ts>.png` | Only saved if an exception is thrown |

   (`open_short` produces identically named files with `open_short_` prefix.)

## Directory Structure

```
.
├── webhook_server.py      # Main server code
├── requirements.txt       # Python dependencies
├── cache/                # Cache directory
│   ├── instance_1/       # Cache for instance 1
│   ├── instance_2/       # Cache for instance 2
│   └── ...
├── chrome_user_data/     # Chrome profiles
│   ├── instance_1/       # Profile for instance 1
│   ├── instance_2/       # Profile for instance 2
│   └── ...
└── ublock-origin-built/  # uBlock Origin extension
```

## Webhook Configuration

Configure your TradingView webhook alerts to send POST requests to:
```
http://your_server_ip:port/webhook
```

The webhook should send a JSON payload with the following structure:
```json
{
    "symbol": "SOL_USDT",
    "action": "buy"  // or "sell"
}
```

## Webhook Actions

The server supports the following webhook actions:

### Navigate
```json
{
    "action": "navigate",
    "url": "https://www.mexc.com"
}
```

### Open Long
```json
{
    "action": "open_long",
    "symbol": "SOL_USDT",
    "leverage": 1,
    "quantity": 100
}
```

### Open Short
```json
{
    "action": "open_short",
    "symbol": "SOL_USDT",
    "leverage": 1,
    "quantity": 100
}
```

### Close Long
```json
{
    "action": "close_long",
    "symbol": "SOL_USDT",
    "quantity": 100
}
```

### Close Short
```json
{
    "action": "close_short",
    "symbol": "SOL_USDT",
    "quantity": 100
}
```

### Close Position (Legacy)
```json
{
    "action": "close_position",
    "symbol": "SOL_USDT"
}
```

## Response Format

The server responds with JSON in the following format:

```json
{
    "status": "success",
    "message": "Action completed successfully"
}
```

Or in case of an error:

```json
{
    "status": "error",
    "message": "Error description"
}
```

## Implementation Details

The webhook server is built using:

- **Quart**: An async web framework for Python
- **Playwright**: Browser automation library
- **uBlock Origin**: Browser extension for ad blocking and improved performance

The server uses a persistent browser context with the uBlock Origin extension enabled to provide a better trading experience by blocking ads and unnecessary content.

### Execution Strategy

All trade actions use **pure Playwright clicks** with `force=True` to ensure reliability even when MEXC renders feature-announcement popovers over the order form.

- **Opening Positions** (`open_long`, `open_short`):
  1. Dismiss all blocking overlays via `close_popovers()` (handles `ant-modal`, `ant-popover`, `ant-popover-v5`, `handleWrapper`/SpotEarnGuide, and similar MEXC guide widgets)
  2. Click the Open tab
  3. Click the Market tab
  4. Click the 100% quantity slider mark (`span:nth-child(5)`)
  5. Click the Open Long / Open Short button
  - All clicks use `force=True` so any overlay that survives step 1 cannot intercept them

- **Closing Positions** (`close_long`, `close_short`):
  - Same overlay-dismissal step, then Playwright clicks through the Close tab flow
  - Automatic retry with page reload on failure

- **Stability / Self-healing**:
  - Serializes all browser actions to avoid overlapping Playwright operations on a single page
  - Automatically reloads the MEXC futures page periodically to mitigate long-lived SPA slowdown
  - Automatically restarts the browser context if Playwright reports `Target crashed` / `Target ... has been closed`

## Development Tools

- `debug_selectors.py`: Use this script when you need to debug Playwright selectors for the MEXC interface
- `mexc_with_proxy.py`: Use this script to test MEXC functionality with a proxy server

## Troubleshooting

- If you encounter issues with the browser not starting, check that Chrome is installed and the uBlock Origin extension is properly built
- Check the logs for any error messages
- If startup fails with a login-wait timeout, finish logging in faster or bump `MEXC_LOGIN_WAIT_SECONDS` in `.env`
- If the browser profile gets corrupted (e.g. `Target page, context or browser has been closed` on launch), delete `chrome_user_data/instance_<N>/` and start again — you'll need to log in manually once
- Make sure each instance has its own unique port number
- If trades are not executing, restart the instance with `--debug` to capture screenshots and form-state logs — see the [Debug Mode](#debug-mode) section above for how to interpret them
- If `close_popovers` logs `Blocking modal/popover may still be visible after retries`, a new MEXC popup type may have appeared; capture its HTML and add its container class to the overlay selectors and close-button selectors in `close_popovers()`

## Security Notes

- Use HTTPS in production environments
- Consider implementing additional security measures for the webhook endpoint
- Because login is manual, this project never stores or reads your MEXC credentials. Your session lives only inside `chrome_user_data/instance_<N>/` — treat that directory as sensitive and add it to `.gitignore` if it isn't already