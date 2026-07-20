#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import pickle
import argparse
import signal
import subprocess
import time
from playwright.async_api import async_playwright
from quart import Quart, request, jsonify
from dotenv import load_dotenv

# Load .env file if present (credentials, config overrides)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Quart app
app = Quart(__name__)

# Global variables to store browser and page instances
playwright_instance = None
context = None
page = None
current_symbol = "SOL_USDT"  # Track the current symbol

# Land on the "#info" tab by default: the order-entry panel stays fully
# functional, but the chart component never mounts, so its kline stream and
# chart bundle never load — meaningfully less data through a metered proxy.
FUTURES_PAGE_HASH = "#info"

def futures_url(symbol):
    return f"https://www.mexc.com/futures/{symbol}?type=linear_swap{FUTURES_PAGE_HASH}"

# Concurrency + stability controls (single page, so serialize actions)
ACTION_LOCK = asyncio.Lock()
BROWSER_LOCK = asyncio.Lock()
NEEDS_BROWSER_RESTART = False

# Auto-refresh to prevent the MEXC SPA from slowing down over time
LAST_RELOAD_TS = 0.0
ACTION_COUNT_SINCE_RELOAD = 0
RELOAD_EVERY_SECONDS = 30 * 60   # 30 minutes
RELOAD_EVERY_ACTIONS = 150       # refresh after N actions
_periodic_reload_task = None     # background task for time-based reload
ACTION_TIMEOUT_MS = 5000         # default timeout for locator actions/clicks

# The Playwright *driver* (a long-lived Node.js process, separate from Chrome
# itself) slowly accumulates memory over very long uptimes and can eventually
# hit "JavaScript heap out of memory". A plain page.reload() never resets
# this, because it's the same driver process/connection the whole time. So we
# periodically do a *full* browser+driver restart to reclaim that memory
# before it ever gets close to the crash ceiling.
BROWSER_RESTART_EVERY_SECONDS = 6 * 60 * 60   # 6 hours
LAST_BROWSER_RESTART_TS = 0.0

_restart_in_progress = False     # reentrancy guard for restart_browser()
_pending_restart_task = None     # background task used to react to crash/close immediately

# Selectors that indicate the MEXC futures trading interface is loaded AND the user is logged in.
# We combine the legacy id/class selectors with the modern data-testid attributes that the rest
# of the code actually uses, so detection still works after MEXC DOM refactors.
TRADING_INTERFACE_SELECTOR = (
    "#mexc_contract_v, "
    ".contract-trade-order-form, "
    "[data-testid='contract-trade-order-form'], "
    "[data-testid='contract-trade-order-form-tab-open'], "
    "[data-testid='contract-trade-open-long-btn']"
)


def _looks_like_target_crash(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "target crashed" in msg
        or "target page, context or browser has been closed" in msg
        or "browsercontext.close: target page, context or browser has been closed" in msg
        or "page has been closed" in msg
    )


async def restart_browser(reason: str):
    """Hard restart Playwright + browser context. Used after Target crashed/closed."""
    global playwright_instance, context, page, NEEDS_BROWSER_RESTART, current_symbol, _restart_in_progress

    if _restart_in_progress:
        # A restart triggered by our own context.close() below (which fires a
        # "close" event on the old page) or an overlapping trigger. No-op.
        logger.debug(f"Restart already in progress; ignoring duplicate trigger (reason={reason})")
        return
    _restart_in_progress = True

    try:
        logger.warning(f"Restarting browser (reason={reason})")
        NEEDS_BROWSER_RESTART = False

        # Best-effort cookie save, but don't let it fail the restart.
        try:
            if context:
                cookies = await context.cookies()
                cache_data["cookies"] = cookies
                save_cache()
        except Exception as e:
            logger.debug(f"Could not save cookies before restart: {e}")

        # Close existing context/page
        try:
            if context:
                await context.close()
        except Exception as e:
            logger.debug(f"Context close during restart failed/ignored: {e}")

        # Stop Playwright driver
        try:
            if playwright_instance:
                await playwright_instance.stop()
        except Exception as e:
            logger.debug(f"Playwright stop during restart failed/ignored: {e}")

        playwright_instance = None
        context = None
        page = None
        current_symbol = None

        await initialize_browser()
    finally:
        _restart_in_progress = False


def _schedule_immediate_restart(reason: str):
    """Fire off a restart as soon as the current in-flight action (if any) releases
    ACTION_LOCK, instead of waiting for the next webhook call or the 30-minute
    periodic loop. This is what actually fixes crashed/closed pages sitting blank
    for a long time before anyone notices."""
    global _pending_restart_task

    if _pending_restart_task and not _pending_restart_task.done():
        return

    async def _do_restart():
        async with ACTION_LOCK:
            await ensure_browser_ready()

    _pending_restart_task = asyncio.create_task(_do_restart())


def _cleanup_stale_chrome_profile_lock(user_data_dir: str):
    """Kill any leftover Chrome process still holding this profile and clear its
    singleton lock files.

    A Playwright driver crash (e.g. the Node heap OOM we've seen after long
    uptimes) can leave the underlying Chrome process orphaned rather than
    cleanly killed. That zombie process keeps holding the profile's
    SingletonLock, so the next launch_persistent_context() either fails or the
    OS hands the "open window" request off to the zombie process instead of
    our new Playwright-controlled one — which is why a stale, logged-out,
    blank window can show up after a crash/restart.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={user_data_dir}"],
            capture_output=True, text=True, timeout=5
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        current_pid = os.getpid()
        for pid in pids:
            if pid == current_pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                logger.info(f"Killed stale Chrome process (pid={pid}) still holding profile lock")
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"Could not kill stale Chrome process {pid}: {e}")
        if pids:
            time.sleep(0.5)  # give the OS a moment to release the lock file
    except Exception as e:
        logger.debug(f"Stale Chrome cleanup (pgrep) failed/skipped: {e}")

    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(user_data_dir, lock_name)
        try:
            if os.path.islink(lock_path) or os.path.exists(lock_path):
                os.remove(lock_path)
                logger.debug(f"Removed stale {lock_name} from {user_data_dir}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"Could not remove {lock_path}: {e}")


async def ensure_browser_ready():
    """Ensure we have a usable page/context; restart if it crashed/closed."""
    global page, context, NEEDS_BROWSER_RESTART

    # Decide what to do while holding the lock, but perform heavy restart/init outside it.
    need_restart = False
    need_init = False

    async with BROWSER_LOCK:
        if NEEDS_BROWSER_RESTART:
            need_restart = True
        elif page is None or context is None:
            need_init = True
        else:
            try:
                if page.is_closed():
                    need_restart = True
            except Exception:
                need_restart = True

    if need_restart:
        await restart_browser("browser/page unhealthy")
    elif need_init:
        await initialize_browser()


async def maybe_reload_page(force: bool = False, reason: str = ""):
    """Periodically reload the futures page to clear SPA slowness."""
    global LAST_RELOAD_TS, ACTION_COUNT_SINCE_RELOAD, current_symbol, NEEDS_BROWSER_RESTART

    if page is None:
        return

    now = time.time()
    should_reload = (
        force
        or (LAST_RELOAD_TS and (now - LAST_RELOAD_TS) > RELOAD_EVERY_SECONDS)
        or (ACTION_COUNT_SINCE_RELOAD >= RELOAD_EVERY_ACTIONS)
    )
    if not should_reload:
        return

    try:
        logger.info(
            f"Reloading page to prevent slowdown (reason={reason or 'scheduled'}, "
            f"actions_since_reload={ACTION_COUNT_SINCE_RELOAD}, "
            f"age_seconds={int(now - LAST_RELOAD_TS) if LAST_RELOAD_TS else 'n/a'})"
        )
        # Bandwidth guard: if a manual session left the browser on the FULL trade
        # UI (no #info hash -> chart, order book and every ticker stream mounted,
        # ~2-3 GB/day through a metered proxy), point the URL back at the light
        # #info variant BEFORE the hard reload so the fresh load stays light.
        # Non-futures pages (login/captcha) are left alone.
        try:
            _cur = page.url or ""
            if "/futures/" in _cur and FUTURES_PAGE_HASH not in _cur:
                _sym = current_symbol or \
                    _cur.split("/futures/")[1].split("?")[0].split("#")[0] or "SOL_USDT"
                _light = futures_url(_sym)
                logger.info(f"Reload returns to the bandwidth-light page: {_light}")
                await page.goto(_light, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.debug(f"light-page redirect skipped: {e}")

        # Hard-reload equivalent (Cmd+Shift+R): temporarily disable cache and reload.
        # Playwright doesn't expose an explicit "bypass cache" flag for reload, so we
        # use the Chromium DevTools Protocol (CDP) Network domain.
        cdp = None
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception as e:
            logger.debug(f"CDP cache-bypass not available; falling back to normal reload: {e}")

        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        finally:
            if cdp is not None:
                try:
                    await cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
                except Exception:
                    pass
                # Detach the CDP session explicitly — otherwise these accumulate in
                # the Playwright driver process over hundreds of reloads and
                # contribute to the slow Node.js heap growth that eventually OOMs.
                try:
                    await cdp.detach()
                except Exception:
                    pass
        await asyncio.sleep(1)
        current_symbol = None  # force ensure_symbol_page() to re-validate
        LAST_RELOAD_TS = now
        ACTION_COUNT_SINCE_RELOAD = 0
    except Exception as e:
        logger.warning(f"Page reload failed, will restart browser: {e}")
        NEEDS_BROWSER_RESTART = True
        await restart_browser(f"reload failed: {e}")


async def _periodic_reload_loop():
    """Background task: reload the MEXC page every 30 minutes to prevent SPA slowdown,
    and do a full browser+driver restart every BROWSER_RESTART_EVERY_SECONDS to reclaim
    the Playwright driver's memory before it can grow large enough to OOM (which we've
    seen happen after ~2 days of continuous uptime).
    """
    global _periodic_reload_task
    while True:
        try:
            await asyncio.sleep(RELOAD_EVERY_SECONDS)
            async with ACTION_LOCK:
                await ensure_browser_ready()
                due_for_hard_restart = (
                    LAST_BROWSER_RESTART_TS
                    and (time.time() - LAST_BROWSER_RESTART_TS) > BROWSER_RESTART_EVERY_SECONDS
                )
                if due_for_hard_restart:
                    await restart_browser("periodic_hard_restart")
                else:
                    await maybe_reload_page(force=True, reason="periodic_timer")
        except asyncio.CancelledError:
            logger.info("Periodic reload task cancelled")
            break
        except Exception as e:
            logger.warning(f"Periodic reload loop error: {e}")


# Instance configuration
INSTANCE_ID = None
PORT = None
DEBUG_MODE = False  # enabled with --debug flag; adds screenshots and form-state logging

# Browser networking / rendering (set from CLI args or env in __main__)
PROXY = None          # dict(server=..., username=..., password=...) or None
HEADLESS = False      # --headless; view the live page any time at /view
BLOCK_HEAVY = None    # abort image/media/font requests to save proxy bandwidth;
                      # None = auto (on when a proxy is configured).
                      # Captcha/verification/login URLs are always exempt.

# Cache configuration
CACHE_DIR = None
CACHE_FILE = None

# Cache data structure
cache_data = {
    "visited_urls": set(),
    "cookies": None,
    "last_symbol": None
}

def initialize_instance_config(instance_id, port):
    """Initialize instance-specific configuration."""
    global INSTANCE_ID, PORT, CACHE_DIR, CACHE_FILE

    INSTANCE_ID = instance_id
    PORT = port

    # Set up instance-specific cache directory
    CACHE_DIR = os.path.join("cache", f"instance_{instance_id}")
    CACHE_FILE = os.path.join(CACHE_DIR, "webhook_cache.pkl")

    # Ensure cache directory exists
    os.makedirs(CACHE_DIR, exist_ok=True)

    logger.info(f"Initialized instance {instance_id} with port {port}")

def load_cache():
    """Load cache data from file if it exists."""
    global cache_data

    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'rb') as f:
                loaded_cache = pickle.load(f)
                # Update only existing keys to avoid errors with new cache structure
                for key in cache_data:
                    if key in loaded_cache:
                        cache_data[key] = loaded_cache[key]
                logger.info("Cache loaded successfully")
    except Exception as e:
        logger.error(f"Error loading cache: {str(e)}")

def save_cache():
    """Save cache data to file."""
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache_data, f)
        logger.info("Cache saved successfully")
    except Exception as e:
        logger.error(f"Error saving cache: {str(e)}")

async def initialize_browser():
    """Initialize the browser and page for Playwright."""
    global playwright_instance, context, page, LAST_RELOAD_TS, ACTION_COUNT_SINCE_RELOAD, NEEDS_BROWSER_RESTART, LAST_BROWSER_RESTART_TS

    # Load cache
    load_cache()

    # Path to the uBlock Origin extension
    ublock_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "ublock-origin-built"))

    # Check if uBlock Origin extension exists and should be used
    use_ublock = os.path.exists(ublock_path) and os.path.exists(os.path.join(ublock_path, "manifest.json"))

    if use_ublock:
        logger.info(f"Loading uBlock Origin extension from {ublock_path}")
    else:
        logger.info("Running without uBlock Origin extension")

    # Use an instance-specific directory for Chrome user data
    user_data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "chrome_user_data", f"instance_{INSTANCE_ID}"))
    if not os.path.exists(user_data_dir):
        os.makedirs(user_data_dir)

    logger.info(f"Using Chrome user data directory: {user_data_dir}")

    # Make sure no orphaned Chrome process from a previous driver crash is still
    # holding this profile's lock before we try to launch into it.
    _cleanup_stale_chrome_profile_lock(user_data_dir)

    try:
        # If Playwright is already started (e.g. restart path), stop it first.
        if playwright_instance:
            try:
                await playwright_instance.stop()
            except Exception:
                pass
            playwright_instance = None

        playwright_instance = await async_playwright().start()
        logger.info("Playwright started successfully")

        # Launch browser with more detailed logging
        logger.info("Launching browser...")

        # Prepare launch arguments
        launch_args = [
            '--no-first-run',
            '--no-default-browser-check',
            # Full desktop render size so the whole trading UI is laid out and
            # captured — headless otherwise defaults to a small window and the
            # /screenshot view gets clipped.
            '--window-size=1600,1000',
            '--force-device-scale-factor=1',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--disable-setuid-sandbox',
            '--no-sandbox',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            # Stealth: suppress the "Chrome is being controlled by automated test software" banner
            '--disable-blink-features=AutomationControlled',
        ]

        if PROXY:
            # IMPORTANT: launch_persistent_context() silently IGNORES the proxy=
            # dict for Chromium in several Playwright versions, so Chrome went
            # straight to the real IP. Passing --proxy-server explicitly is the
            # reliable fix; the proxy= dict below still supplies the credentials.
            _proxy_server = PROXY['server']
            launch_args.extend([
                f'--proxy-server={_proxy_server}',
                # never send localhost/loopback through the proxy
                '--proxy-bypass-list=<-loopback>',
                # No proxy bypass paths: WebRTC/STUN can leak the real IP over UDP
                # and QUIC is UDP-based — force everything onto the proxied TCP path.
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                '--webrtc-ip-handling-policy=disable_non_proxied_udp',
                '--disable-quic',
            ])

        # Add uBlock Origin if available
        if use_ublock:
            launch_args.extend([
                f'--disable-extensions-except={ublock_path}',
                f'--load-extension={ublock_path}'
            ])

        if PROXY:
            logger.info(f"Routing browser traffic through proxy {PROXY['server']}"
                        f"{' (authenticated)' if PROXY.get('username') else ''}")
        context = await playwright_instance.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=HEADLESS,
            proxy=(PROXY or None),
            args=launch_args,
            # Modern Chrome UA — older UAs are flagged by GeeTest / reCAPTCHA
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36",
            ignore_default_args=['--enable-automation'],
            no_viewport=True
        )
        logger.info("Browser launched successfully")

        if PROXY:
            # Trust-but-verify: compare the BROWSER's egress IP against this
            # machine's real (direct) IP. If they match, the proxy isn't being
            # applied — log it LOUD so it can never slip through to live trading.
            try:
                import urllib.request
                try:
                    direct_ip = urllib.request.urlopen(
                        "https://api.ipify.org", timeout=10).read().decode().strip()
                except Exception:
                    direct_ip = None
                _p = await context.new_page()
                await _p.goto("https://api.ipify.org?format=text", timeout=20000)
                egress = (await _p.inner_text("body")).strip()
                await _p.close()
                if direct_ip and egress == direct_ip:
                    logger.error("=" * 68)
                    logger.error(f"PROXY LEAK: browser egress IP {egress} == your REAL IP. "
                                 "Proxy is NOT being used. DO NOT trade live.")
                    logger.error("=" * 68)
                else:
                    logger.info("=" * 68)
                    logger.info(f"PROXY OK — browser egress IP: {egress}"
                                + (f" (real IP {direct_ip} is hidden)" if direct_ip else ""))
                    logger.info("=" * 68)
            except Exception as e:
                logger.warning(f"PROXY CHECK failed (could not fetch egress IP): {e}")

        # Bandwidth saver (mainly for metered residential proxies): abort images,
        # media and fonts. NEVER blocks captcha/verification/login resources —
        # the manual login slider needs its puzzle images.
        block_heavy = BLOCK_HEAVY if BLOCK_HEAVY is not None else bool(PROXY)
        if block_heavy:
            _exempt = ("captcha", "geetest", "verify", "login", "signin", "gee")

            async def _block_heavy_route(route):
                try:
                    req = route.request
                    u = req.url.lower()
                    if any(s in u for s in _exempt):
                        await route.continue_()
                        return
                    if req.resource_type in ("image", "media", "font"):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            await context.route("**/*", _block_heavy_route)
            logger.info("Heavy-resource blocking ON (images/media/fonts aborted; "
                        "captcha/login URLs exempt)")

        # Inject stealth script into every new page/frame to hide common Playwright tells
        # (navigator.webdriver, missing chrome runtime, permission query quirk, etc.)
        try:
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Plugin', description: '', filename: '' }))
                });
                window.chrome = window.chrome || { runtime: {} };
                const origQuery = window.navigator.permissions && window.navigator.permissions.query;
                if (origQuery) {
                    window.navigator.permissions.query = (p) =>
                        p && p.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : origQuery(p);
                }
            """)
            logger.info("Stealth init script installed")
        except Exception as e:
            logger.warning(f"Could not install stealth init script: {e}")

        # Default action timeout (user requested faster failure for click actions)
        try:
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(60000)
        except Exception:
            pass

        # Restore cookies if available
        if cache_data["cookies"]:
            await context.add_cookies(cache_data["cookies"])
            logger.info("Cookies restored from cache")

        page = await context.new_page()
        logger.info("New page created successfully")

        try:
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(60000)
        except Exception:
            pass

        # Crash/close watchdogs
        def _on_page_crash():
            global NEEDS_BROWSER_RESTART
            NEEDS_BROWSER_RESTART = True
            logger.error("Playwright page crashed; restarting now")
            _schedule_immediate_restart("page crashed")

        def _on_page_close():
            global NEEDS_BROWSER_RESTART
            NEEDS_BROWSER_RESTART = True
            logger.error("Playwright page closed; restarting now")
            _schedule_immediate_restart("page closed")

        try:
            page.on("crash", _on_page_crash)
            page.on("close", _on_page_close)
        except Exception:
            pass

        # Navigate to MEXC with more efficient loading strategy
        logger.info("Navigating to MEXC...")
        try:
            # Navigate directly to the futures page
            await page.goto(futures_url("SOL_USDT"),
                          timeout=60000,
                          wait_until="domcontentloaded")
            logger.info("Futures page loaded successfully")

            # Startup readiness: keep this fast. We'll validate selectors later during actual actions.
            try:
                await page.wait_for_selector("body", timeout=10000)
                logger.info("Body element found")

                # Optional screenshot for debugging startup state
                if os.environ.get("MEXC_TD_DEBUG_SCREENSHOT") == "1":
                    await page.screenshot(path="page_state.png")
                    logger.info("Page state screenshot saved as page_state.png")

                # Wait for the trading interface to be ready.
                # If the user needs to log in, they have up to MEXC_LOGIN_WAIT_SECONDS
                # (default 300s / 5 minutes) to do so manually in the opened browser window.
                login_wait = int(os.environ.get("MEXC_LOGIN_WAIT_SECONDS", "300"))
                try:
                    await page.wait_for_selector(
                        TRADING_INTERFACE_SELECTOR,
                        timeout=2000,
                    )
                    logger.info("Trading interface ready — already logged in")
                except Exception:
                    logger.info(
                        f"Trading interface not visible — please log in manually in the "
                        f"browser window. Waiting up to {login_wait}s..."
                    )
                    await page.wait_for_selector(
                        TRADING_INTERFACE_SELECTOR,
                        timeout=login_wait * 1000,
                    )
                    logger.info("Trading interface ready after manual login")

            except Exception as selector_error:
                logger.error(f"Error waiting for page elements: {str(selector_error)}")
                # Take a screenshot for debugging
                try:
                    await page.screenshot(path="error_screenshot.png")
                    logger.info("Error screenshot saved as error_screenshot.png")
                except Exception as screenshot_error:
                    logger.error(f"Could not take screenshot: {str(screenshot_error)}")
                raise

        except Exception as nav_error:
            logger.error(f"Navigation error: {str(nav_error)}")
            # Try to get page content to see what's loaded
            try:
                content = await page.content()
                logger.info(f"Page content: {content[:500]}...")  # Log first 500 chars
            except Exception as content_error:
                logger.error(f"Could not get page content: {str(content_error)}")
            raise

        # Update cache with current symbol
        cache_data["last_symbol"] = "SOL_USDT"
        current_symbol = "SOL_USDT"
        LAST_RELOAD_TS = time.time()
        LAST_BROWSER_RESTART_TS = time.time()
        ACTION_COUNT_SINCE_RELOAD = 0
        NEEDS_BROWSER_RESTART = False

        logger.info("Browser initialized successfully")
        return page

    except Exception as e:
        logger.error(f"Error initializing browser: {str(e)}")
        if context:
            try:
                await context.close()
                logger.info("Browser context closed after error")
            except Exception as close_error:
                logger.error(f"Error closing browser context: {str(close_error)}")
        try:
            if playwright_instance:
                await playwright_instance.stop()
        except Exception:
            pass
        playwright_instance = None
        return None

async def verify_url():
    """Verify that the current page URL matches the required URL before taking actions."""
    global current_symbol
    required_url = futures_url("SOL_USDT")
    try:
        current_url = page.url
        logger.info(f"Current URL: {current_url}")
        logger.info(f"Required URL: {required_url}")
        
        # Check if URL matches (allowing for query parameter variations)
        url_matches = (current_url == required_url or 
                       required_url in current_url or 
                       current_url.startswith("https://www.mexc.com/futures/SOL_USDT"))
        
        if not url_matches:
            logger.warning(f"URL mismatch! Current: {current_url}, Required: {required_url}")
            # Force navigation by temporarily resetting current_symbol
            # This ensures ensure_symbol_page will navigate even if current_symbol is already SOL_USDT
            original_symbol = current_symbol
            current_symbol = None
            
            try:
                logger.info("Navigating to required URL using ensure_symbol_page...")
                try:
                    await ensure_symbol_page("SOL_USDT")
                except Exception as ensure_error:
                    logger.warning(f"ensure_symbol_page failed: {str(ensure_error)}")
                    logger.info("Attempting direct navigation as fallback...")
                    # Try direct navigation with JavaScript as fallback
                    try:
                        await page.evaluate(f"window.location.href = '{required_url}'")
                        await asyncio.sleep(2)  # Brief wait for navigation to start
                        # Wait for trading interface elements instead of networkidle
                        try:
                            await page.wait_for_selector(TRADING_INTERFACE_SELECTOR, timeout=5000)
                            logger.info("Trading interface ready after JavaScript navigation")
                        except Exception:
                            logger.warning("Trading interface not immediately ready, but proceeding")
                        current_symbol = "SOL_USDT"
                        cache_data["last_symbol"] = "SOL_USDT"
                        save_cache()
                    except Exception as js_error:
                        logger.error(f"JavaScript navigation also failed: {str(js_error)}")
                        raise ensure_error  # Raise original error
                
                # Verify we're on the correct URL after navigation
                final_url = page.url
                logger.info(f"Final URL after navigation: {final_url}")
                
                final_url_matches = (final_url == required_url or 
                                   required_url in final_url or 
                                   final_url.startswith("https://www.mexc.com/futures/SOL_USDT"))
                
                if not final_url_matches:
                    error_msg = f"Failed to navigate to required URL. Current: {final_url}, Required: {required_url}"
                    logger.error(error_msg)
                    return {"status": "error", "message": error_msg}
                
                logger.info("Successfully navigated to required URL")
            except Exception as nav_error:
                # Restore original symbol if navigation failed
                current_symbol = original_symbol
                raise
        else:
            logger.info("URL verification passed")
        return {"status": "success"}
    except Exception as e:
        error_msg = f"Error verifying URL: {str(e)}"
        logger.error(error_msg)
        return {"status": "error", "message": error_msg}

async def close_popovers(max_attempts=5):
    """Close blocking modal dialogs and popovers before trading actions."""
    try:
        no_progress_streak = 0  # consecutive attempts where clicked == 0
        for attempt in range(max_attempts):
            close_result = await page.evaluate("""() => {
                const isVisible = (el) => !!el && !!(
                    el.offsetWidth || el.offsetHeight || el.getClientRects().length
                );

                // Collect all overlay types that can block trading actions.
                // Includes ant-modal, ant-popover/v5, and MEXC-specific guide wrappers
                // such as handle_handleWrapper (SpotEarnGuide) that intercept pointer events.
                const seen = new Set();
                const candidates = [
                    '.ant-modal-wrap',
                    '.ant-modal-mask',
                    '[role="dialog"][aria-modal="true"]',
                    '.ant-popover',
                    '.ant-popover-v5',
                    '[class*="handleWrapper"]',
                    '[class*="GuidePopup"]',
                    '[class*="EarnGuide"]',
                ].flatMap(sel => Array.from(document.querySelectorAll(sel)))
                 .filter(el => {
                     if (seen.has(el) || !isVisible(el)) return false;
                     seen.add(el);
                     return true;
                 });

                // Try these selectors in order to find a clickable dismiss control.
                // Covers standard ant-design buttons, MEXC div-based close icons
                // (GuidePopupModal_closeIcon, AiTabPopover_closeIconWrap), and the
                // SpotEarnGuide / handle_handleWrapper close pattern.
                const closeSelectors = [
                    '[class*="closeIconWrap"]',
                    '[class*="closeIcon"]',
                    '[class*="CloseIcon"]',
                    '[class*="handleClose"]',
                    '[class*="close-btn"]',
                    '[class*="closeBtn"]',
                    'button[aria-label="Close"]',
                    '.ant-modal-close',
                    '.ant-modal-close-x',
                    '.ant-modal-footer .ant-btn-primary',
                    '.ant-modal-footer .ant-btn',
                    '.ant-modal-body .ant-btn-primary',
                    '.ant-modal-content .ant-btn-primary',
                    '.ant-popover-inner-content .ant-btn',
                    '.ant-popover-v5-inner-content .ant-btn',
                ];

                let clicked = 0;
                for (const overlay of candidates) {
                    for (const sel of closeSelectors) {
                        const btn = overlay.querySelector(sel);
                        if (btn && isVisible(btn)) {
                            try { btn.click(); clicked++; break; } catch (_) {}
                        }
                    }
                }

                const stillVisible = [
                    '.ant-modal-wrap',
                    '.ant-modal-mask',
                    '[role="dialog"][aria-modal="true"]',
                    '.ant-popover',
                    '.ant-popover-v5',
                    '[class*="handleWrapper"]',
                    '[class*="GuidePopup"]',
                    '[class*="EarnGuide"]',
                ].flatMap(sel => Array.from(document.querySelectorAll(sel)))
                 .some(isVisible);

                return { clicked, overlays: candidates.length, stillBlocking: stillVisible };
            }""")

            overlays_detected = close_result.get("overlays", 0)
            clicked = close_result.get("clicked", 0)
            still_blocking = close_result.get("stillBlocking", False)

            if overlays_detected > 0:
                logger.info(
                    f"Modal/popover close attempt {attempt + 1}: "
                    f"clicked={clicked}, overlays_detected={overlays_detected}"
                )

            if not still_blocking:
                if attempt > 0 or clicked > 0:
                    logger.info("All overlays cleared")
                return

            if clicked > 0:
                no_progress_streak = 0
                # Give the UI a moment to animate/dismiss before the next check.
                await asyncio.sleep(0.1)
            elif overlays_detected > 0:
                no_progress_streak += 1
                # Try Escape on the first stuck attempt; if a second attempt also
                # makes no progress, the remaining overlays are undismissable —
                # force=True clicks will bypass them anyway, so stop wasting time.
                if no_progress_streak == 1:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    logger.debug("No further progress dismissing overlays; proceeding with force clicks")
                    break
            else:
                return

        logger.warning("Blocking modal/popover may still be visible after retries")

    except Exception as e:
        logger.debug(f"Error checking/closing popovers: {str(e)}")
        # Don't fail the action if popup closing fails

async def ensure_symbol_page(symbol):
    """Ensure we're on the correct symbol page, only navigate if needed."""
    global current_symbol

    # Check if we've already visited this symbol page
    url = futures_url(symbol)
    current_url = page.url if page else ""
    already_on_symbol_url = current_url.startswith(f"https://www.mexc.com/futures/{symbol}")

    if current_symbol != symbol and not already_on_symbol_url:
        logger.info(f"Navigating to {symbol} page...")
        try:
            await page.goto(url, timeout=90000, wait_until="domcontentloaded")
            logger.info("Page navigation completed, checking for trading interface elements...")
        except Exception as goto_error:
            logger.warning(f"Navigation with domcontentloaded failed: {str(goto_error)}")
            # Try without wait_until as fallback
            logger.info("Attempting navigation without wait_until...")
            await page.goto(url, timeout=90000)
        
        # Single fast readiness check (avoid multi-selector wait loops).
        try:
            await page.wait_for_selector(
                "#mexc_contract_v, .contract-trade-order-form, .ant-segmented-item",
                timeout=2500
            )
        except Exception:
            logger.debug("Trading interface not fully visible yet, proceeding with action flow")
        
        current_symbol = symbol

        # Update cache
        cache_data["last_symbol"] = symbol
        cache_data["visited_urls"].add(url)
        save_cache()

        logger.info(f"Now on {symbol} page")
    else:
        if already_on_symbol_url:
            current_symbol = symbol
        logger.info(f"Already on {symbol} page")

async def _debug_screenshot(label: str):
    """Save a timestamped debug screenshot. No-op unless DEBUG_MODE is on."""
    if not DEBUG_MODE:
        return
    try:
        path = f"debug_{label}_{int(time.time())}.png"
        await page.screenshot(path=path)
        logger.info(f"Screenshot saved: {path}")
    except Exception as e:
        logger.debug(f"Could not save screenshot '{label}': {e}")


async def _log_form_state(label: str):
    """Log which key elements are present and visible. No-op unless DEBUG_MODE is on."""
    if not DEBUG_MODE:
        return
    try:
        state = await page.evaluate("""() => {
            const isVisible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const slider = document.querySelector('.ant-slider-v2-step > span:nth-child(5)');
            const allSliderSpans = document.querySelectorAll('.ant-slider-v2-step > span');
            const overlays = [...document.querySelectorAll(
                '.ant-modal-wrap,.ant-modal-mask,[role="dialog"][aria-modal="true"],.ant-popover,.ant-popover-v5'
            )].filter(isVisible).map(el => el.className.split(' ')[0]);
            return {
                openTab:     isVisible(document.querySelector('[data-testid="contract-trade-order-form-tab-open"]')),
                openLongBtn: isVisible(document.querySelector('[data-testid="contract-trade-open-long-btn"]')),
                openShortBtn:isVisible(document.querySelector('[data-testid="contract-trade-open-short-btn"]')),
                sliderSpanCount: allSliderSpans.length,
                sliderNth5:  !!slider,
                sliderNth5Visible: isVisible(slider),
                visibleOverlays: overlays,
            };
        }""")
        logger.info(f"Form state [{label}]: {state}")
    except Exception as e:
        logger.debug(f"Could not read form state '{label}': {e}")


# ---- leverage control (selectors verified against the live DOM 2026-07-18) ----
# The order strip shows two identical-looking controls: LeverageEdit_long = the
# margin-mode button ("Isolated"), LeverageEdit_short = the LEVERAGE button
# ("9X"). Clicking the wrong one opens the margin-mode dialog.
LEVERAGE_BTN_SEL = 'div[class*="LeverageEdit_short"]'
LEVERAGE_INPUT_SEL = 'input[class*="LeverageProgress_leverageInput"]'


async def get_current_leverage():
    """Read the leverage shown on the order strip (e.g. '9X' -> 9), or None."""
    import re as _re
    try:
        t = (await page.locator(LEVERAGE_BTN_SEL).first.inner_text(timeout=5000)).strip()
        m = _re.search(r"(\d+)\s*X", t, _re.I)
        return int(m.group(1)) if m else None
    except Exception:
        return None


async def set_leverage(target):
    """Set the futures leverage for the current symbol via the leverage dialog.
    No-op if the strip already shows the target. Fails LOUDLY (and closes the
    dialog) rather than leaving the account on the wrong leverage."""
    target = max(1, int(target))
    cur = await get_current_leverage()
    if cur == target:
        return {"status": "success", "message": f"leverage already {target}X",
                "leverage": target}
    logger.info(f"Setting leverage: {cur}X -> {target}X")
    try:
        await close_popovers()
        await page.locator(LEVERAGE_BTN_SEL).first.click()
        inp = page.locator(LEVERAGE_INPUT_SEL).first
        await inp.wait_for(state="visible", timeout=10000)
        await inp.fill(str(target))
        await asyncio.sleep(0.2)
        if (await inp.input_value()).strip() != str(target):
            # React-controlled input rejected fill: select-all + retype
            await inp.click(click_count=3)
            await page.keyboard.type(str(target))
            await asyncio.sleep(0.2)
            if (await inp.input_value()).strip() != str(target):
                raise RuntimeError(
                    f"leverage input shows '{await inp.input_value()}' not '{target}'")
        confirm = page.locator(
            '.ant-modal-wrap:visible button:has-text("Confirm")').last
        await confirm.click()
        await inp.wait_for(state="hidden", timeout=10000)   # dialog closed
        await asyncio.sleep(0.3)
        got = await get_current_leverage()
        if got != target:
            raise RuntimeError(f"strip shows {got}X after confirm")
        logger.info(f"Leverage set to {target}X")
        return {"status": "success", "message": f"leverage set to {target}X",
                "leverage": target}
    except Exception as e:
        logger.error(f"set_leverage({target}) failed: {e}")
        # never leave the dialog open — it would block the order buttons
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass
        await _debug_screenshot("set_leverage_error")
        return {"status": "error", "message": f"set_leverage failed: {e}"}


async def open_long(symbol="SOL_USDT", leverage=1, quantity=100):
    """Open a long position using Playwright clicks."""
    try:
        url_check = await verify_url()
        if url_check["status"] != "success":
            return url_check

        await ensure_symbol_page(symbol)
        await close_popovers()

        await _log_form_state("open_long_start")
        await _debug_screenshot("open_long_1_start")

        logger.info("Step 1: clicking Open tab")
        open_tab = page.get_by_test_id("contract-trade-order-form-tab-open")
        await open_tab.click(force=True)
        await asyncio.sleep(0.1)

        logger.info("Step 2: clicking Market tab")
        market_tab = page.get_by_test_id("contract-trade-order-form").get_by_role("tab", name="Market")
        await market_tab.click(force=True)
        await asyncio.sleep(0.1)

        if leverage >= 1:
            lev_res = await set_leverage(leverage)
            if lev_res["status"] != "success":
                # do NOT open at whatever leverage happens to be set
                return {"status": "error",
                        "message": f"entry aborted — {lev_res['message']}"}

        await _debug_screenshot("open_long_2_after_tabs")

        logger.info("Step 3: clicking 100% quantity slider")
        quantity_slider = page.locator(".ant-slider-v2-step > span:nth-child(5)").first
        await quantity_slider.click(force=True)
        await asyncio.sleep(0.1)

        await _debug_screenshot("open_long_3_after_slider")

        logger.info("Step 4: clicking Open Long button")
        open_long_btn = page.get_by_test_id("contract-trade-open-long-btn")
        await open_long_btn.click(force=True)
        await asyncio.sleep(0.4)

        await _debug_screenshot("open_long_4_after_button")

        logger.info(f"Opened long position for {symbol} with leverage {leverage}x and quantity {quantity}%")
        return {"status": "success", "message": f"Opened long position for {symbol}"}
    except Exception as e:
        logger.error(f"Failed to open long position: {str(e)}")
        await _debug_screenshot("open_long_error")
        return {"status": "error", "message": f"Failed to open long position: {str(e)}"}

async def open_short(symbol="SOL_USDT", leverage=1, quantity=100):
    """Open a short position using Playwright clicks."""
    try:
        url_check = await verify_url()
        if url_check["status"] != "success":
            return url_check

        await ensure_symbol_page(symbol)
        await close_popovers()

        await _log_form_state("open_short_start")
        await _debug_screenshot("open_short_1_start")

        logger.info("Step 1: clicking Open tab")
        open_tab = page.get_by_test_id("contract-trade-order-form-tab-open")
        await open_tab.click(force=True)
        await asyncio.sleep(0.1)

        logger.info("Step 2: clicking Market tab")
        market_tab = page.get_by_test_id("contract-trade-order-form").get_by_role("tab", name="Market")
        await market_tab.click(force=True)
        await asyncio.sleep(0.1)

        if leverage >= 1:
            lev_res = await set_leverage(leverage)
            if lev_res["status"] != "success":
                # do NOT open at whatever leverage happens to be set
                return {"status": "error",
                        "message": f"entry aborted — {lev_res['message']}"}

        await _debug_screenshot("open_short_2_after_tabs")

        logger.info("Step 3: clicking 100% quantity slider")
        quantity_slider = page.locator(".ant-slider-v2-step > span:nth-child(5)").first
        await quantity_slider.click(force=True)
        await asyncio.sleep(0.1)

        await _debug_screenshot("open_short_3_after_slider")

        logger.info("Step 4: clicking Open Short button")
        open_short_btn = page.get_by_test_id("contract-trade-open-short-btn")
        await open_short_btn.click(force=True)
        await asyncio.sleep(0.4)

        await _debug_screenshot("open_short_4_after_button")

        logger.info(f"Opened short position for {symbol} with leverage {leverage}x and quantity {quantity}%")
        return {"status": "success", "message": f"Opened short position for {symbol}"}
    except Exception as e:
        logger.error(f"Failed to open short position: {str(e)}")
        await _debug_screenshot("open_short_error")
        return {"status": "error", "message": f"Failed to open short position: {str(e)}"}

async def close_long(symbol="SOL_USDT", quantity=100):
    """Close a long position."""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # Verify URL before taking action
            url_check = await verify_url()
            if url_check["status"] != "success":
                return url_check
            
            # Ensure we're on the correct symbol page
            await ensure_symbol_page(symbol)
            
            # Close any popovers that might block actions
            await close_popovers()

            # Wait a moment for page to stabilize (helps prevent duplicate elements)
            await asyncio.sleep(0.5)

            # "Close" tab — segmented control item
            close_tab = (
                page.get_by_test_id("contract-trade-order-form-tab-close").first
                if await page.locator('[data-testid="contract-trade-order-form-tab-close"]').count() > 0
                else page.locator('.ant-segmented-item').filter(has_text="Close").first
            )
            await close_tab.click()
            await asyncio.sleep(0.3)

            # "Market" order type tab
            market_tab = page.get_by_role("tab", name="Market").first
            await market_tab.click()

            # Quantity slider — try scoped selector first, then fallback
            quantity_slider = page.locator(
                "#mexc_contract_v_close_position .ant-slider-v2-step span:last-child"
            ).last
            if await quantity_slider.count() == 0:
                quantity_slider = page.locator(".ant-slider-v2-step span:last-child").last
            await quantity_slider.click()

            # "Close Long" button
            close_long_btn = (
                page.get_by_test_id("contract-trade-close-long-btn")
                if await page.locator('[data-testid="contract-trade-close-long-btn"]').count() > 0
                else page.get_by_role("button", name="Close Long").first
            )
            await close_long_btn.click()

            # Wait for confirmation
            await page.wait_for_timeout(2000)

            logger.info(f"Closed long position for {symbol} with quantity {quantity}%")
            return {"status": "success", "message": f"Closed long position for {symbol}"}
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Attempt {attempt + 1} failed to close long position: {error_msg}")
            
            # If this is the last attempt, return error
            if attempt == max_retries - 1:
                logger.error(f"Failed to close long position after {max_retries} attempts: {error_msg}")
                return {"status": "error", "message": f"Failed to close long position: {error_msg}"}
            
            # Refresh page and retry
            logger.info(f"Refreshing page and retrying close_long (attempt {attempt + 2}/{max_retries})...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)  # Wait for page to stabilize after refresh
                # Reset symbol to force navigation
                global current_symbol
                current_symbol = None
            except Exception as refresh_error:
                logger.error(f"Failed to refresh page: {str(refresh_error)}")
                return {"status": "error", "message": f"Failed to refresh page: {str(refresh_error)}"}

async def close_short(symbol="SOL_USDT", quantity=100):
    """Close a short position."""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # Verify URL before taking action
            url_check = await verify_url()
            if url_check["status"] != "success":
                return url_check
            
            # Ensure we're on the correct symbol page
            await ensure_symbol_page(symbol)
            
            # Close any popovers that might block actions
            await close_popovers()

            # Wait a moment for page to stabilize (helps prevent duplicate elements)
            await asyncio.sleep(0.5)

            # "Close" tab — segmented control item
            close_tab = (
                page.get_by_test_id("contract-trade-order-form-tab-close").first
                if await page.locator('[data-testid="contract-trade-order-form-tab-close"]').count() > 0
                else page.locator('.ant-segmented-item').filter(has_text="Close").first
            )
            await close_tab.click()
            await asyncio.sleep(0.3)

            # "Market" order type tab
            market_tab = page.get_by_role("tab", name="Market").first
            await market_tab.click()

            # Quantity slider
            quantity_slider = page.locator(
                "#mexc_contract_v_close_position .ant-slider-v2-step span:last-child"
            ).last
            if await quantity_slider.count() == 0:
                quantity_slider = page.locator(".ant-slider-v2-step span:last-child").last
            await quantity_slider.click()

            # "Close Short" button
            close_short_btn = (
                page.get_by_test_id("contract-trade-close-short-btn")
                if await page.locator('[data-testid="contract-trade-close-short-btn"]').count() > 0
                else page.get_by_role("button", name="Close Short").first
            )
            await close_short_btn.click()

            # Wait for confirmation
            await page.wait_for_timeout(2000)

            logger.info(f"Closed short position for {symbol} with quantity {quantity}%")
            return {"status": "success", "message": f"Closed short position for {symbol}"}
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Attempt {attempt + 1} failed to close short position: {error_msg}")
            
            # If this is the last attempt, return error
            if attempt == max_retries - 1:
                logger.error(f"Failed to close short position after {max_retries} attempts: {error_msg}")
                return {"status": "error", "message": f"Failed to close short position: {error_msg}"}
            
            # Refresh page and retry
            logger.info(f"Refreshing page and retrying close_short (attempt {attempt + 2}/{max_retries})...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)  # Wait for page to stabilize after refresh
                # Reset symbol to force navigation
                global current_symbol
                current_symbol = None
            except Exception as refresh_error:
                logger.error(f"Failed to refresh page: {str(refresh_error)}")
                return {"status": "error", "message": f"Failed to refresh page: {str(refresh_error)}"}

async def close_position(symbol="SOL_USDT"):
    """Close a position (legacy function) using hybrid approach: JS first, then Playwright clicks."""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # Verify URL before taking action
            url_check = await verify_url()
            if url_check["status"] != "success":
                return url_check
            
            # Ensure we're on the correct symbol page
            await ensure_symbol_page(symbol)
            
            # Close any popovers that might block actions
            await close_popovers()

            # Wait a moment for page to stabilize (helps prevent duplicate elements)
            await asyncio.sleep(0.5)

            # Try JavaScript execution first (faster)
            js_success = await page.evaluate("""() => {
                return new Promise((resolve) => {
                    try {
                        const closeTab = document.querySelector('[data-testid="contract-trade-order-form-tab-close"]')
                            || Array.from(document.querySelectorAll('.ant-segmented-item')).find(
                                   el => el.textContent.trim() === 'Close');

                        const closePositionContainer = document.querySelector('#mexc_contract_v_close_position')
                            || document.querySelector('.futures-new-order-wrap')
                            || document.querySelector('[data-testid="contract-trade-order-form"]');
                        const marketTab = closePositionContainer
                            ? closePositionContainer.querySelector('[role="tab"]')
                              || Array.from(closePositionContainer.querySelectorAll('*')).find(
                                     el => el.textContent && el.textContent.trim() === 'Market')
                            : Array.from(document.querySelectorAll('[role="tab"]')).find(
                                   el => el.textContent.trim() === 'Market');

                        const closeBtn = document.querySelector('[data-testid="contract-trade-close-position-btn"]')
                            || Array.from(document.querySelectorAll('button')).find(
                                   btn => /^Close Position$/i.test(btn.textContent.trim()));

                        if (!closeTab || !closeBtn) {
                            console.error('Missing elements:', {closeTab: !!closeTab, closeBtn: !!closeBtn});
                            resolve(false);
                            return;
                        }

                        closeTab.click();

                        setTimeout(() => {
                            if (marketTab) {
                                marketTab.click();
                                setTimeout(() => {
                                    if (closeBtn.offsetParent !== null) {
                                        closeBtn.click();
                                        resolve(true);
                                    } else {
                                        console.error('Close button not visible');
                                        resolve(false);
                                    }
                                }, 200);
                            } else {
                                if (closeBtn.offsetParent !== null) {
                                    closeBtn.click();
                                    resolve(true);
                                } else {
                                    console.error('Close button not visible');
                                    resolve(false);
                                }
                            }
                        }, 300);
                    } catch (e) {
                        console.error('JS execution failed:', e);
                        resolve(false);
                    }
                });
            }""")

            if js_success:
                await asyncio.sleep(0.6)

            if not js_success:
                logger.info("JS execution failed, falling back to Playwright clicks")

                close_tab = (
                    page.get_by_test_id("contract-trade-order-form-tab-close").first
                    if await page.locator('[data-testid="contract-trade-order-form-tab-close"]').count() > 0
                    else page.locator('.ant-segmented-item').filter(has_text="Close").first
                )
                await close_tab.click()
                await asyncio.sleep(0.3)

                market_tab = page.get_by_role("tab", name="Market").first
                await market_tab.click()

                close_btn = (
                    page.get_by_test_id("contract-trade-close-position-btn")
                    if await page.locator('[data-testid="contract-trade-close-position-btn"]').count() > 0
                    else page.get_by_role("button", name="Close Position").first
                )
                await close_btn.click()

            # Wait for confirmation
            await page.wait_for_timeout(2000)

            logger.info(f"Closed position for {symbol}")
            return {"status": "success", "message": f"Closed position for {symbol}"}
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Attempt {attempt + 1} failed to close position: {error_msg}")
            
            # If this is the last attempt, return error
            if attempt == max_retries - 1:
                logger.error(f"Failed to close position after {max_retries} attempts: {error_msg}")
                return {"status": "error", "message": f"Failed to close position: {error_msg}"}
            
            # Refresh page and retry
            logger.info(f"Refreshing page and retrying close_position (attempt {attempt + 2}/{max_retries})...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)  # Wait for page to stabilize after refresh
                # Reset symbol to force navigation
                global current_symbol
                current_symbol = None
            except Exception as refresh_error:
                logger.error(f"Failed to refresh page: {str(refresh_error)}")
                return {"status": "error", "message": f"Failed to refresh page: {str(refresh_error)}"}

async def navigate(url):
    """Navigate to a specific URL."""
    try:
        # Check if we've already visited this URL
        if url in cache_data["visited_urls"]:
            logger.info(f"URL {url} already visited, using cached data")

        await page.goto(url, timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=60000)

        # Update cache
        cache_data["visited_urls"].add(url)
        save_cache()

        logger.info(f"Navigated to {url}")
        return {"status": "success", "message": f"Navigated to {url}"}
    except Exception as e:
        logger.error(f"Navigation failed: {str(e)}")
        return {"status": "error", "message": f"Navigation failed: {str(e)}"}

@app.before_serving
async def startup():
    """Initialize the browser before serving requests."""
    global page, _periodic_reload_task
    try:
        # Create a task for browser initialization
        init_task = asyncio.create_task(initialize_browser())

        # Wait for the task to complete
        page = await init_task

        if page is None:
            logger.error("Failed to initialize browser")
            raise Exception("Browser initialization failed")

        logger.info("Browser initialized successfully")

        # Start background task to reload page every 30 minutes
        _periodic_reload_task = asyncio.create_task(_periodic_reload_loop())
        logger.info("Periodic reload task started (every 30 minutes)")
    except asyncio.CancelledError:
        logger.error("Browser initialization was cancelled")
        raise
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}")
        raise

@app.after_serving
async def shutdown():
    """Close the browser after serving requests."""
    global playwright_instance, context, _periodic_reload_task

    # Cancel the periodic reload task
    if _periodic_reload_task and not _periodic_reload_task.done():
        _periodic_reload_task.cancel()
        try:
            await _periodic_reload_task
        except asyncio.CancelledError:
            pass
        _periodic_reload_task = None

    if context:
        try:
            # Save cookies before closing
            cookies = await context.cookies()
            cache_data["cookies"] = cookies
            save_cache()
        except Exception as e:
            logger.debug(f"Could not save cookies during shutdown: {e}")

        try:
            await context.close()
            logger.info("Browser closed")
        except Exception as e:
            # Don't crash lifespan on shutdown if the target is already closed/crashed.
            logger.debug(f"Ignoring shutdown close error (likely already closed): {e}")

    try:
        if playwright_instance:
            await playwright_instance.stop()
    except Exception as e:
        logger.debug(f"Ignoring playwright stop error during shutdown: {e}")
    finally:
        playwright_instance = None

@app.route('/screenshot', methods=['GET'])
async def screenshot_route():
    """Current page as PNG — lets you 'see the Chrome window' while headless."""
    try:
        if page is None:
            return jsonify({"status": "error", "message": "no page yet"}), 503
        png = await page.screenshot(full_page=False)
        from quart import Response
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/view', methods=['GET'])
async def view_route():
    """Live view of the (possibly headless) browser: auto-refreshing screenshot."""
    return ("""<!doctype html><title>MEXC executor — live view</title>
<body style="margin:0;background:#0d1117;color:#c9d1d9;font:13px sans-serif">
<div style="padding:6px 10px">MEXC executor instance — live view (refreshes every 2s).
Headless browser screenshot; to interact (e.g. login captcha), restart the server
without --headless.</div>
<img id=s src="/screenshot" style="max-width:100%;display:block">
<script>setInterval(()=>{document.getElementById('s').src='/screenshot?t='+Date.now()},2000)</script>
</body>""", 200, {"Content-Type": "text/html"})


@app.route('/set_leverage', methods=['POST'])
async def set_leverage_route():
    """Manually set the leverage (testing / recovery): {"leverage": 8}"""
    data = await request.get_json()
    lev = int((data or {}).get("leverage", 0))
    if lev < 1:
        return jsonify({"status": "error", "message": "leverage must be >= 1"}), 400
    async with ACTION_LOCK:
        await ensure_browser_ready()
        result = await set_leverage(lev)
    return jsonify(result), (200 if result["status"] == "success" else 500)


@app.route('/debug/eval', methods=['POST'])
async def debug_eval():
    """Localhost-only maintenance hatch: evaluate JS on the live page and return
    the result. Used to verify selectors against MEXC's real DOM before trusting
    them in the order path."""
    data = await request.get_json()
    js = (data or {}).get("js", "")
    if not js:
        return jsonify({"status": "error", "message": "no js"}), 400
    async with ACTION_LOCK:
        await ensure_browser_ready()
        try:
            res = await page.evaluate(js)
            return jsonify({"status": "success", "result": res})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/webhook', methods=['POST'])
async def webhook():
    """Handle incoming webhook requests."""
    global NEEDS_BROWSER_RESTART, ACTION_COUNT_SINCE_RELOAD
    try:
        # Log request headers
        logger.info("Request headers:")
        logger.info(dict(request.headers))

        # Get the raw request data
        raw_data = await request.get_data()
        logger.info(f"Raw request data: {raw_data.decode('utf-8')}")

        # Try to parse JSON data
        try:
            data = await request.get_json()
            # Some callers send empty bodies; Quart returns None in that case.
            if data is None:
                logger.info("Received null/empty request body, ignoring")
                return jsonify({"status": "success", "message": "Null request ignored"}), 200
            logger.info(f"Parsed JSON data: {json.dumps(data, indent=2)}")
        except Exception as json_error:
            logger.error(f"Error parsing JSON: {str(json_error)}")
            # If this is a null request from TradingView, just return success
            if "null" in raw_data.decode('utf-8').lower():
                logger.info("Received null request from TradingView, ignoring")
                return jsonify({"status": "success", "message": "Null request ignored"}), 200
            return jsonify({"status": "error", "message": "Invalid JSON data received"}), 400

        if not data or 'action' not in data:
            logger.error("Invalid webhook payload")
            return jsonify({"status": "error", "message": "Invalid webhook payload"}), 400

        action = data['action']
        logger.info(f"Processing action: {action}")

        # Serialize all browser interactions to a single Playwright page.
        async with ACTION_LOCK:
            await ensure_browser_ready()
            await maybe_reload_page(reason=f"pre_action:{action}")

            if action == 'navigate':
                url = data.get('url', 'https://www.mexc.com')
                result = await navigate(url)

            elif action == 'open_long':
                symbol = data.get('symbol', 'SOL_USDT')
                leverage = int(data.get('leverage', 1))
                quantity = int(data.get('quantity', 100))
                logger.info(f"Calling open_long with symbol={symbol}, leverage={leverage}, quantity={quantity}")
                result = await open_long(symbol, leverage, quantity)

            elif action == 'open_short':
                symbol = data.get('symbol', 'SOL_USDT')
                leverage = int(data.get('leverage', 1))
                quantity = int(data.get('quantity', 100))
                logger.info(f"Calling open_short with symbol={symbol}, leverage={leverage}, quantity={quantity}")
                result = await open_short(symbol, leverage, quantity)

            elif action == 'close_long':
                symbol = data.get('symbol', 'SOL_USDT')
                quantity = int(data.get('quantity', 100))
                logger.info(f"Calling close_long with symbol={symbol}, quantity={quantity}")
                result = await close_long(symbol, quantity)
                logger.info(f"close_long completed with result: {result}")

            elif action == 'close_short':
                symbol = data.get('symbol', 'SOL_USDT')
                quantity = int(data.get('quantity', 100))
                logger.info(f"Calling close_short with symbol={symbol}, quantity={quantity}")
                result = await close_short(symbol, quantity)

            elif action == 'close_position':
                symbol = data.get('symbol', 'SOL_USDT')
                logger.info(f"Calling close_position with symbol={symbol}")
                result = await close_position(symbol)

            else:
                logger.error(f"Unknown action: {action}")
                return jsonify({"status": "error", "message": f"Unknown action: {action}"}), 400

            # If the browser crashed/closed, restart immediately so the next command doesn't pile on.
            try:
                if isinstance(result, dict) and result.get("status") == "error" and _looks_like_target_crash(Exception(result.get("message", ""))):
                    NEEDS_BROWSER_RESTART = True
                    await restart_browser(f"error_result:{result.get('message','')}")
            except Exception:
                pass

            # Count trading actions toward scheduled reloads.
            try:
                if action not in ("navigate",):
                    ACTION_COUNT_SINCE_RELOAD += 1
            except Exception:
                pass

        logger.info(f"Action completed, sending response: {result}")
        return jsonify(result)

    except Exception as e:
        # If we see a crash/closed signal here, mark restart so future requests recover quickly.
        if _looks_like_target_crash(e):
            NEEDS_BROWSER_RESTART = True
            try:
                await restart_browser(f"webhook_exception:{e}")
            except Exception:
                pass
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({"status": "error", "message": f"Webhook error: {str(e)}"}), 500

if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    import asyncio

    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Run MEXC trading bot instance')
    parser.add_argument('--instance', type=int, required=True, help='Instance ID (1, 2, 3, etc.)')
    parser.add_argument('--port', type=int, required=True, help='Port number for this instance')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode (screenshots, form-state logging)')
    parser.add_argument('--headless', action='store_true',
                        help='Run Chrome headless (watch it live at http://127.0.0.1:<port>/view). '
                             'Run WITHOUT this flag when you need to log in (captcha slider needs a real window).')
    parser.add_argument('--proxy-server', default=os.environ.get('MEXC_PROXY_SERVER'),
                        help='Proxy for ALL browser traffic, e.g. http://isp.decodo.com:10001 '
                             '(or set MEXC_PROXY_SERVER)')
    parser.add_argument('--proxy-username', default=os.environ.get('MEXC_PROXY_USERNAME'),
                        help='Proxy auth username (or MEXC_PROXY_USERNAME)')
    parser.add_argument('--proxy-password', default=os.environ.get('MEXC_PROXY_PASSWORD'),
                        help='Proxy auth password (or MEXC_PROXY_PASSWORD)')
    parser.add_argument('--block-heavy', dest='block_heavy', action='store_true', default=None,
                        help='Abort image/media/font requests to save proxy bandwidth '
                             '(default: on when a proxy is set; captcha/login always exempt)')
    parser.add_argument('--no-block-heavy', dest='block_heavy', action='store_false',
                        help='Disable heavy-resource blocking')
    args = parser.parse_args()

    # Initialize instance configuration
    initialize_instance_config(args.instance, args.port)

    if args.debug:
        DEBUG_MODE = True
        logger.info("Debug mode enabled: screenshots and form-state logging are active")

    HEADLESS = args.headless
    BLOCK_HEAVY = args.block_heavy
    if args.proxy_server:
        PROXY = dict(server=args.proxy_server)
        if args.proxy_username:
            PROXY["username"] = args.proxy_username
        if args.proxy_password:
            PROXY["password"] = args.proxy_password
    else:
        # Fallback: proxy_config.json next to this file (gitignored).
        _pc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "proxy_config.json")
        if os.path.exists(_pc_path):
            try:
                with open(_pc_path) as _f:
                    _pc = json.load(_f)
                # Only inspect the actual credential fields — NOT the _readme,
                # whose help text contains the word "FILL_IN".
                _filled = _pc.get("server") and not any(
                    "FILL_IN" in str(_pc.get(k, "")) for k in ("server", "username", "password"))
                if _filled:
                    PROXY = {k: _pc[k] for k in ("server", "username", "password")
                             if _pc.get(k)}
                    logger.info(f"Proxy loaded from {_pc_path}")
                else:
                    logger.warning(f"{_pc_path} present but not filled in — "
                                   "running WITHOUT proxy")
            except Exception as _e:
                logger.error(f"Could not read {_pc_path}: {_e} — running WITHOUT proxy")
    if HEADLESS:
        logger.info(f"Headless mode: live view at http://127.0.0.1:{args.port}/view")

    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    config.worker_class = "asyncio"
    config.workers = 1  # Use single worker to avoid browser conflicts
    config.keep_alive_timeout = 120
    config.websocket_ping_interval = 30
    config.websocket_timeout = 120
    # Auto-login (slider + TOTP) can take 30–60 s by itself, so allow a generous
    # window for the startup hook to finish without tripping hypercorn's default.
    config.startup_timeout = 300

    try:
        asyncio.run(serve(app, config))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {str(e)}")
        raise
