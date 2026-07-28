#!/usr/bin/env python3
"""Build <coin>[_spot]_5min.parquet from the 1-minute files (exact OHLCV
aggregation). 1-min holes become 5-min holes; gap segmentation handles them."""
import os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

def build(src, dst):
    df = pd.read_parquet(os.path.join(DATA, src))
    df = df.set_index("t").sort_index()
    g = df.resample("5min")
    cols = dict(open=g["open"].first(), high=g["high"].max(),
                low=g["low"].min(), close=g["close"].last(),
                volume=g["volume"].sum())
    if "trades_count" in df.columns:
        cols["trades_count"] = g["trades_count"].sum(min_count=1)
    out = pd.DataFrame(cols)
    if "trades_count" not in out.columns:
        out["trades_count"] = pd.NA
    out = out.dropna(subset=["close"]).reset_index()
    out.to_parquet(os.path.join(DATA, dst), index=False)
    print(f"{dst}: {len(out)} bars ({out['t'].min()} -> {out['t'].max()})")

if __name__ == "__main__":
    coins = sys.argv[1:] or ["sol", "btc", "eth", "doge", "xrp", "sui"]
    for c in coins:
        for sfx in ("", "_spot"):
            src = f"{c}{sfx}_1min.parquet"
            if os.path.exists(os.path.join(DATA, src)):
                build(src, f"{c}{sfx}_5min.parquet")
