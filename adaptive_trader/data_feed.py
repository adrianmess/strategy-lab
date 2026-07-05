#!/usr/bin/env python3
"""Live kline feed from MEXC public contract API (no auth needed).

Fetches Min1 and Min3 klines for the configured symbol and maintains a
rolling history long enough for all indicators/regime windows (>= 35 days).
"""
import time
import logging
import requests
import pandas as pd

logger = logging.getLogger(__name__)

BASE = "https://contract.mexc.com/api/v1/contract/kline"
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
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.df3 = None
        self.df1 = None

    def backfill(self):
        end = int(time.time())
        start = end - HISTORY_DAYS * 86400
        logger.info("Backfilling %d days of klines...", HISTORY_DAYS)
        self.df1 = fetch_range(self.symbol, "Min1", start, end)
        self.df3 = resample_3m(self.df1)
        logger.info("Backfill done: %d 3m bars, %d 1m bars", len(self.df3), len(self.df1))

    def update(self):
        """Fetch the most recent bars and merge. Returns True if a new CLOSED
        3m bar arrived since last call."""
        end = int(time.time())
        start = end - 3600  # last hour is plenty
        new1 = _fetch(self.symbol, "Min1", start, end)
        prev_last = self.df3["t"].iloc[-1] if len(self.df3) else None
        self.df1 = (pd.concat([self.df1, new1], ignore_index=True)
                    .drop_duplicates("t", keep="last").sort_values("t").reset_index(drop=True))
        # trim
        cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=HISTORY_DAYS)
        self.df1 = self.df1[self.df1["t"] >= cutoff].reset_index(drop=True)
        self.df3 = resample_3m(self.df1)
        return prev_last is None or self.df3["t"].iloc[-1] > prev_last

    def closed_bars(self):
        """All 3m bars that are certainly closed (drop the in-progress bar)."""
        now = pd.Timestamp.utcnow().tz_localize(None)
        df = self.df3
        return df[df["t"] + pd.Timedelta(minutes=3) <= now].reset_index(drop=True)

    def last_price(self) -> float:
        return float(self.df1["close"].iloc[-1])
