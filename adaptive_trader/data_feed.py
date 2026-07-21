#!/usr/bin/env python3
"""Live kline feed from MEXC public contract API (no auth needed).

Fetches Min1 and Min3 klines for the configured symbol and maintains a
rolling history long enough for all indicators/regime windows (>= 35 days).
"""
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


def _fetch(symbol: str, interval: str, start: int, end: int) -> pd.DataFrame:
    url = f"{BASE}/{symbol}?interval={interval}&start={start}&end={end}"
    r = requests.get(url, timeout=15)
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


def fetch_range(symbol: str, interval: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    """Paginate through [start_ts, end_ts] (unix seconds). MEXC has no Min3
    interval, so 3m bars are resampled from Min1."""
    if interval == "Min3":
        df1 = fetch_range(symbol, "Min1", start_ts, end_ts)
        return resample_3m(df1)
    step = {"Min1": 60}[interval] * MAX_PER_REQ
    out = []
    cur = start_ts
    while cur < end_ts:
        chunk_end = min(cur + step, end_ts)
        df = _fetch(symbol, interval, cur, chunk_end)
        if len(df):
            out.append(df)
        cur = chunk_end
        time.sleep(0.15)  # be polite
    if not out:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume"])
    df = pd.concat(out, ignore_index=True).drop_duplicates("t").sort_values("t")
    return df.reset_index(drop=True)


def resample_3m(df1: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m bars into 3m bars aligned to :00/:03/:06 (TradingView-style).
    Only emits 3m bars whose window is fully covered or partially traded —
    same as exchange bars (bar exists if any 1m bar exists in the window)."""
    if not len(df1):
        return df1.copy()
    g = df1.set_index("t").resample("3min", label="left", closed="left")
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna(subset=["open"]).reset_index()
    return out[["t", "open", "high", "low", "close", "volume"]]


class Feed:
    def __init__(self, symbol: str, live: bool = True, anchored: bool = False):
        self.symbol = symbol
        self.df3 = None
        self.df1 = None
        self.anchored = anchored
        self.trim_ok = False    # trader sets True when flat (anchored mode)
        self.live = LivePrice(symbol) if live else None

    def backfill(self):
        end = int(time.time())
        start = end - HISTORY_DAYS * 86400
        logger.info("Backfilling %d days of klines...", HISTORY_DAYS)
        self.df1 = fetch_range(self.symbol, "Min1", start, end)
        self.df3 = resample_3m(self.df1)
        logger.info("Backfill done: %d 3m bars, %d 1m bars", len(self.df3), len(self.df1))
        if self.live:
            self.live.start()   # begin streaming real-time ticks

    def update(self):
        """Fetch the most recent bars and merge. Returns True if a new CLOSED
        3m bar arrived since last call."""
        end = int(time.time())
        start = end - 3600  # last hour is plenty
        new1 = _fetch(self.symbol, "Min1", start, end)
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
        self.df3 = resample_3m(self.df1)
        return prev_last is None or self.df3["t"].iloc[-1] > prev_last

    def closed_bars(self):
        """All 3m bars that are certainly closed (drop the in-progress bar)."""
        now = pd.Timestamp.utcnow().tz_localize(None)
        df = self.df3
        return df[df["t"] + pd.Timedelta(minutes=3) <= now].reset_index(drop=True)

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
