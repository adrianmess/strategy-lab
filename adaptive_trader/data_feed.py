#!/usr/bin/env python3
"""Live kline feed from MEXC public contract API (no auth needed).

Fetches Min1 and Min3 klines for the configured symbol and maintains a
rolling history long enough for all indicators/regime windows (>= 35 days).
"""
import os
import time
import json
import asyncio
import logging
import threading
import requests
import pandas as pd

logger = logging.getLogger(__name__)

BASE = "https://contract.mexc.com/api/v1/contract/kline"
WS_URL = "wss://contract.mexc.com/edge"

# ---- dedicated proxy pool (adaptive_trader/proxy_pool.json, optional) ----
# Each port is a fixed exit IP. Feeds pick a stable port per (symbol, tf) so
# every feed keeps its own IP identity and MEXC rate limits never interact.
# The WebSocket tick stream stays direct (public, not rate-limited the same
# way; kline close is the fallback there anyway).
_POOL = None
def _load_pool():
    global _POOL
    if _POOL is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "proxy_pool.json")
        try:
            pc = json.load(open(p))
            _POOL = [f"http://{pc['username']}:{pc['password']}@"
                     f"{pc['host']}:{port}" for port in pc["ports"]]
        except Exception:
            _POOL = []
    return _POOL

def pool_proxy(key=None, index=None):
    """Proxy dict for requests: explicit index, or a DETERMINISTIC hash of
    key (crc32 — python's hash() is per-process randomized)."""
    pool = _load_pool()
    if not pool:
        return None
    if index is None:
        import zlib
        index = zlib.crc32(str(key or "").encode()) % len(pool)
    url = pool[index % len(pool)]
    return {"http": url, "https": url}


class LivePrice:
    """Real-time last price from MEXC's contract WebSocket, maintained in a
    background thread. Auto-reconnects. If websockets is unavailable or the
    socket is down, get() returns None and callers fall back to the kline close
    — so this is strictly an enhancement, never a regression."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._price = None
        self._ts = 0.0
        self._stop = False
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="LivePrice",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def _run(self):
        try:
            asyncio.run(self._loop())
        except Exception as e:
            logger.warning("LivePrice thread ended: %s (falling back to klines)", e)

    async def _loop(self):
        try:
            import websockets
        except Exception as e:
            logger.warning("websockets not available (%s); live price disabled, "
                           "using 1m kline close", e)
            return
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, ping_interval=None,
                                              close_timeout=5) as ws:
                    await ws.send(json.dumps(
                        {"method": "sub.ticker", "param": {"symbol": self.symbol}}))
                    logger.info("LivePrice: subscribed to %s ticker (WebSocket)",
                                self.symbol)
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for msg in ws:
                            self._handle(msg)
                            if self._stop:
                                break
                    finally:
                        pinger.cancel()
            except Exception as e:
                if not self._stop:
                    logger.warning("LivePrice reconnecting after: %s", e)
                    await asyncio.sleep(2)

    async def _pinger(self, ws):
        try:
            while True:
                await asyncio.sleep(15)
                await ws.send(json.dumps({"method": "ping"}))
        except Exception:
            pass

    def _handle(self, msg):
        try:
            j = json.loads(msg)
        except Exception:
            return
        d = j.get("data")
        if isinstance(d, dict):
            p = d.get("lastPrice", d.get("fairPrice"))
            if p:
                try:
                    self._price = float(p)
                    self._ts = time.time()
                except (TypeError, ValueError):
                    pass

    def get(self, max_age: float = 5.0):
        """Latest tick price if fresher than max_age seconds, else None."""
        if self._price is not None and (time.time() - self._ts) <= max_age:
            return self._price
        return None

    def age(self):
        return (time.time() - self._ts) if self._ts else None
HISTORY_DAYS = 35          # rolling window kept in memory
MAX_PER_REQ = 2000         # MEXC returns up to ~2000 points


def _fetch(symbol: str, interval: str, start: int, end: int,
           proxies=None) -> pd.DataFrame:
    url = f"{BASE}/{symbol}?interval={interval}&start={start}&end={end}"
    # 25s: residential ISP proxies add latency; 15s produced false timeouts
    r = requests.get(url, timeout=25, proxies=proxies)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"MEXC kline error: {j}")
    d = j["data"]
    if not d["time"]:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame({
        "t": pd.to_datetime(d["time"], unit="s", utc=True).tz_localize(None),
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "volume": d["vol"],
    })
    return df


def fetch_range(symbol: str, interval: str, start_ts: int, end_ts: int,
                proxies=None) -> pd.DataFrame:
    """Paginate through [start_ts, end_ts] (unix seconds). MEXC has no Min3
    interval, so 3m bars are resampled from Min1."""
    if interval == "Min3":
        df1 = fetch_range(symbol, "Min1", start_ts, end_ts, proxies=proxies)
        return resample_3m(df1)
    step = {"Min1": 60}[interval] * MAX_PER_REQ
    out = []
    cur = start_ts
    while cur < end_ts:
        chunk_end = min(cur + step, end_ts)
        df = _fetch(symbol, interval, cur, chunk_end, proxies=proxies)
        if len(df):
            out.append(df)
        cur = chunk_end
        time.sleep(0.15)  # be polite
    if not out:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume"])
    df = pd.concat(out, ignore_index=True).drop_duplicates("t").sort_values("t")
    return df.reset_index(drop=True)


def resample_tf(df1: pd.DataFrame, tf_min: int = 3) -> pd.DataFrame:
    """Aggregate 1m bars into tf-minute bars aligned to the hour
    (TradingView-style). tf_min=1 returns the 1m bars as-is. Only emits bars
    whose window is fully covered or partially traded — same as exchange bars
    (bar exists if any 1m bar exists in the window)."""
    if not len(df1) or tf_min == 1:
        return df1.copy()
    g = df1.set_index("t").resample(f"{tf_min}min", label="left", closed="left")
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna(subset=["open"]).reset_index()
    return out[["t", "open", "high", "low", "close", "volume"]]


def resample_3m(df1: pd.DataFrame) -> pd.DataFrame:
    """Back-compat alias (3-minute bars)."""
    return resample_tf(df1, 3)


class Feed:
    def __init__(self, symbol: str, live: bool = True, anchored: bool = False,
                 tf_min: int = 3, proxy_index=None):
        self.symbol = symbol
        self.tf_min = int(tf_min)   # chart bar minutes (1/3/5)
        self.df3 = None             # NOTE: named df3 historically; holds
        self.df1 = None             # tf_min-bars whatever the timeframe
        self.anchored = anchored
        self.trim_ok = False    # trader sets True when flat (anchored mode)
        # dedicated proxy: explicit index, else stable per (symbol, tf) —
        # every feed keeps its own exit IP when a pool file is present
        self.proxies = pool_proxy(key=f"{symbol}@{tf_min}", index=proxy_index)
        if self.proxies:
            logger.info("feed %s/%dm: dedicated proxy #%s", symbol, tf_min,
                        proxy_index if proxy_index is not None else "auto")
        self._perr = 0           # consecutive proxy failures
        self._fb_until = 0.0     # direct-connection fallback window
        self.live = LivePrice(symbol) if live else None

    def _kl(self, interval, start, end):
        """Kline fetch with automatic DIRECT fallback: if the dedicated proxy
        fails repeatedly (e.g. a provider outage like the 2026-08-12 502s),
        data continuity wins — go direct for 5 minutes, then retry the proxy.
        The identity concern is secondary to a live position going blind."""
        px = self.proxies
        if px and time.time() < self._fb_until:
            px = None
        try:
            df = _fetch(self.symbol, interval, start, end, proxies=px)
            if px is not None:
                self._perr = 0
            return df
        except requests.exceptions.RequestException as e:
            s = str(e).lower()
            if px is not None and ("proxy" in s or "tunnel" in s
                                   or "timed out" in s):
                self._perr += 1
                if self._perr >= 2:
                    self._fb_until = time.time() + 300
                    logger.warning("feed %s: proxy failing (%d in a row) — "
                                   "DIRECT fallback for 5 min", self.symbol,
                                   self._perr)
                return _fetch(self.symbol, interval, start, end, proxies=None)
            raise

    def backfill(self):
        end = int(time.time())
        start = end - HISTORY_DAYS * 86400
        logger.info("Backfilling %d days of klines...", HISTORY_DAYS)
        self.df1 = fetch_range(self.symbol, "Min1", start, end,
                               proxies=self.proxies)
        self.df3 = resample_tf(self.df1, self.tf_min)
        logger.info("Backfill done: %d %dm bars, %d 1m bars",
                    len(self.df3), self.tf_min, len(self.df1))
        if self.live:
            self.live.start()   # begin streaming real-time ticks

    def update(self):
        """Fetch the most recent bars and merge. Returns True if a new CLOSED
        chart bar (tf_min minutes) arrived since last call."""
        end = int(time.time())
        start = end - 3600  # last hour is plenty
        new1 = self._kl("Min1", start, end)
        prev_last = self.df3["t"].iloc[-1] if len(self.df3) else None
        self.df1 = (pd.concat([self.df1, new1], ignore_index=True)
                    .drop_duplicates("t", keep="last").sort_values("t").reset_index(drop=True))
        # trim: rolling window by default. ANCHORED mode (router strategies):
        # the window's left edge stays FIXED while it grows, because rolling it
        # re-writes the virtual engines' history and can flip long-held virtual
        # trades (observed in the metax parity test). Re-anchor only when the
        # window has grown 14 extra days AND the trader says it's flat
        # (trim_ok) — so a re-anchor can never happen mid-trade.
        now = pd.Timestamp.utcnow().tz_localize(None)
        if not self.anchored:
            cutoff = now - pd.Timedelta(days=HISTORY_DAYS)
            self.df1 = self.df1[self.df1["t"] >= cutoff].reset_index(drop=True)
        elif (len(self.df1) and self.df1["t"].iloc[0]
                < now - pd.Timedelta(days=HISTORY_DAYS + 14)
                and getattr(self, "trim_ok", False)):
            cutoff = now - pd.Timedelta(days=HISTORY_DAYS)
            logger.info("anchored feed: re-anchoring window to %s (flat)", cutoff)
            self.df1 = self.df1[self.df1["t"] >= cutoff].reset_index(drop=True)
        self.df3 = resample_tf(self.df1, self.tf_min)
        return prev_last is None or self.df3["t"].iloc[-1] > prev_last

    def closed_bars(self):
        """All chart bars that are certainly closed (drop the in-progress bar)."""
        now = pd.Timestamp.utcnow().tz_localize(None)
        df = self.df3
        return df[df["t"] + pd.Timedelta(minutes=self.tf_min) <= now].reset_index(drop=True)

    def last_price(self, max_age: float = 5.0) -> float:
        """Live WebSocket tick if fresh, else the most recent 1m kline close."""
        if self.live is not None:
            p = self.live.get(max_age=max_age)
            if p is not None:
                return p
        return float(self.df1["close"].iloc[-1])

    def price_source(self):
        """'live' if a fresh tick is available, else 'kline' — for logging."""
        if self.live is not None and self.live.get() is not None:
            return "live"
        return "kline"
