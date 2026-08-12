#!/usr/bin/env python3
"""Refresh ALL market data up to now (the panel's 'Refresh market data'
button): every pair, perp AND spot, 3m + 1m via fetch_pair.py (resumable,
extends in place), then rebuild the 5-minute files and clear the legacy
research caches. Per-pair optimizer caches are content-keyed and rebuild
themselves on next use.

Usage:  python3 update_data.py            (from anywhere)
Env:    COINAPI_KEY overrides the default key.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
COINS = ["sol", "btc", "eth", "doge", "xrp", "sui", "hype"]


def run(args):
    print(f"$ {' '.join(args)}", flush=True)
    r = subprocess.run([sys.executable, os.path.join(HERE, args[0])]
                       + args[1:], cwd=HERE)
    if r.returncode:
        print(f"  !! {args[0]} exited rc={r.returncode} — continuing",
              flush=True)
    return r.returncode


def clear_caches():
    for f in ["precomputed.pkl", "variants.pkl", "v6_variants.pkl",
              "scalp_pre.pkl"]:
        for base in [HERE, os.path.join(HERE, "..", "research2"), os.getcwd()]:
            p = os.path.join(base, f)
            if os.path.exists(p):
                os.remove(p)
                print("cleared cache", p, flush=True)
    # CRITICAL (learned 2026-08-12): the optimizer engine pre-caches under
    # optimizer/cache are keyed on INDICATOR settings only, NOT on the candle
    # data — leaving them means every backtest/search silently keeps using
    # the OLD data window. They must go; each rebuilds in a few minutes the
    # first time its pair/tf/market is touched again.
    opt_cache = os.path.join(HERE, "..", "..", "optimizer", "cache")
    n = sz = 0
    for root, _dirs, files in os.walk(opt_cache):
        for f in files:
            if f.endswith(".pkl"):
                p = os.path.join(root, f)
                try:
                    sz += os.path.getsize(p)
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
    print(f"cleared {n} optimizer engine caches ({sz/1e9:.1f} GB) — they "
          f"rebuild lazily on next use (first search/backtest per pair is "
          f"slower). NOTE: any RUNNING campaign will rebuild mid-flight and "
          f"its later legs will train on the refreshed window.", flush=True)


if __name__ == "__main__":
    print(f"=== refreshing {len(COINS)} pairs, perp + spot ===", flush=True)
    rc = 0
    rc |= run(["fetch_pair.py"] + COINS)                       # perp 3m + 1m
    # spot: only the coins that have spot history on disk (all 7 currently)
    spot = [c for c in COINS
            if os.path.exists(os.path.join(DATA, f"{c}_spot_1min.parquet"))]
    if spot:
        rc |= run(["fetch_pair.py", "--market", "spot"] + spot)
    print("=== rebuilding 5-minute files ===", flush=True)
    rc |= run(["gen_5min.py"] + COINS)
    clear_caches()
    print("=== data refresh complete ===", flush=True)
    sys.exit(1 if rc else 0)
