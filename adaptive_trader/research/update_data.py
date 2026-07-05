#!/usr/bin/env python3
"""Extend the CoinAPI kline history (3MIN + 1MIN) up to now, in place.

Usage:  python3 update_data.py            (from anywhere)
Env:    COINAPI_KEY overrides the default key.
Also clears derived caches so optimizers rebuild with fresh data.
"""
import gzip, json, os, sys, urllib.request
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API_KEY = os.environ.get("COINAPI_KEY", "caaddd33-6801-48b9-a48f-642ba05bffb5")
SYMBOL = "MEXCFTS_PERP_SOL_USDT"

def fetch(period, time_start, time_end):
    rows_all = []
    cur = time_start
    while True:
        url = (f"https://rest.coinapi.io/v1/ohlcv/{SYMBOL}/history?period_id={period}"
               f"&time_start={cur}&time_end={time_end}&limit=100000&include_empty_items=false")
        req = urllib.request.Request(url, headers={"X-CoinAPI-Key": API_KEY,
                                                   "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        rows = json.loads(raw)
        if not rows:
            break
        rows_all.extend(rows)
        cur = rows[-1]["time_period_end"]
        if len(rows) < 100000:
            break
    return rows_all

def update(period, fname):
    path = os.path.join(DATA, fname)
    df = pd.read_parquet(path)
    last = pd.Timestamp(df["t"].max())
    start = (last - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
    end = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    rows = fetch(period, start, end)
    if not rows:
        print(f"{fname}: no new data")
        return
    new = pd.DataFrame(rows)
    new["t"] = pd.to_datetime(new["time_period_start"])
    cols = {"price_open": "open", "price_high": "high", "price_low": "low",
            "price_close": "close", "volume_traded": "volume"}
    new = new.rename(columns=cols)
    keep = [c for c in df.columns]
    for c in keep:
        if c not in new.columns:
            new[c] = pd.NA
    new = new[keep]
    if str(df["t"].dtype).startswith("datetime64[ns,"):
        new["t"] = new["t"].dt.tz_convert("UTC") if new["t"].dt.tz else new["t"].dt.tz_localize("UTC")
    merged = (pd.concat([df, new], ignore_index=True)
              .drop_duplicates("t", keep="last").sort_values("t").reset_index(drop=True))
    merged.to_parquet(path, index=False)
    print(f"{fname}: {len(df)} -> {len(merged)} bars (last {merged['t'].max()})")

def clear_caches():
    for f in ["precomputed.pkl", "variants.pkl", "v6_variants.pkl", "scalp_pre.pkl"]:
        for base in [HERE, os.path.join(HERE, "..", "research2"), os.getcwd()]:
            p = os.path.join(base, f)
            if os.path.exists(p):
                os.remove(p)
                print("cleared cache", p)

if __name__ == "__main__":
    update("3MIN", "sol_3min.parquet")
    update("1MIN", "sol_1min.parquet")
    clear_caches()
