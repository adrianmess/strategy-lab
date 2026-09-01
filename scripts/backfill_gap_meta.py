#!/usr/bin/env python3
"""Backfill gap_handling metadata for recently REFRESHED backtest entries.

The refresh worker rebuilt entries without the gap_handling block, so the
dashboard's gaps column showed "unknown" even though every re-run DID use
segmented, contamination-skipped data (that is the engine default and is
not optional). gap_info() is purely a property of the loaded DATASET, so
for entries re-run against the current data files we can reconstruct it
exactly: load each distinct (coin, market, tf) dataset once, ask
backtest_cli.gap_info(), and stamp matching entries retroactive=true.

Only entries created on/after --since (default 2026-08-31, when the
re-run machinery landed) are touched — OLDER entries ran on other data
vintages whose segmentation we must not guess.

Run ON THE MINI:  ~/venv/bin/python3 scripts/backfill_gap_meta.py
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
OPT = os.path.join(REPO, "optimizer")
BTJS = os.path.join(REPO, "dashboard", "backtests.js")

_PROBE = (
    "import json, backtest_cli\n"
    "print('GAPINFO ' + json.dumps(backtest_cli.gap_info()))\n"
)


def gap_for(coin, market, tf):
    env = {**os.environ, "LAB_COIN": coin, "LAB_MARKET": market,
           "LAB_TF": tf}
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=OPT, env=env,
                       capture_output=True, text=True, timeout=600)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("GAPINFO "):
            return json.loads(ln[8:])
    print(f"  probe failed for {coin}/{market}/{tf}: "
          f"{(r.stderr or '')[-160:]}", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-31")
    a = ap.parse_args()
    with open(BTJS + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(BTJS).read()
        entries = json.JSONDecoder().raw_decode(
            txt[txt.index("=") + 1:].lstrip())[0]
        need = [e for e in entries
                if not e.get("gap_handling")
                and str(e.get("created") or "") >= a.since]
        combos = sorted({((e.get("pair") or "SOL_USDT").split("_")[0].lower(),
                          (e.get("market_data") or
                           ("spot" if e.get("mode") == "spot" else "perp")),
                          str(e.get("timeframe") or "3m").rstrip("m"))
                         for e in need})
        print(f"{len(need)} entries missing gap metadata since {a.since}; "
              f"{len(combos)} datasets to probe", flush=True)
        infos = {}
        for c in combos:
            print(f"probing {c} ...", flush=True)
            gi = gap_for(*c)
            if gi:
                infos[c] = dict(gi, retroactive=True)
        fixed = 0
        for e in need:
            c = ((e.get("pair") or "SOL_USDT").split("_")[0].lower(),
                 (e.get("market_data") or
                  ("spot" if e.get("mode") == "spot" else "perp")),
                 str(e.get("timeframe") or "3m").rstrip("m"))
            if c in infos:
                e["gap_handling"] = infos[c]
                if not e.get("gap_mode"):
                    e["gap_mode"] = "skip_contaminated"
                fixed += 1
        if fixed:
            tmp = BTJS + f".tmp{os.getpid()}"
            with open(tmp, "w") as f:
                f.write("window.BACKTESTS = ")
                json.dump(entries, f, default=float)
                f.write(";")
            os.replace(tmp, BTJS)
        print(f"backfilled {fixed} of {len(need)} entries", flush=True)


if __name__ == "__main__":
    main()
