#!/usr/bin/env python3
"""Stamp lev_x + sl_class (see panel/bt_risk.py) onto every published
backtest entry that lacks them. One locked parse+rewrite of backtests.js.

Run ON THE MINI:  ~/venv/bin/python3 scripts/backfill_risk.py
"""
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "panel"))
from bt_risk import risk_of                                  # noqa: E402

BTJS = os.path.join(REPO, "dashboard", "backtests.js")


def main():
    with open(BTJS + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(BTJS).read()
        entries = json.JSONDecoder().raw_decode(
            txt[txt.index("=") + 1:].lstrip())[0]
        stamped = 0
        classes = {}
        for e in entries:
            if e.get("lev_x") and e.get("sl_class"):
                continue
            try:
                lv, slc = risk_of(e)
            except Exception:
                continue
            ch = False
            if lv and not e.get("lev_x"):
                e["lev_x"] = lv
                ch = True
            if slc and not e.get("sl_class"):
                e["sl_class"] = slc
                ch = True
            if ch:
                stamped += 1
                k = (e.get("mode"), slc)
                classes[k] = classes.get(k, 0) + 1
        if stamped:
            tmp = BTJS + f".tmp{os.getpid()}"
            with open(tmp, "w") as f:
                f.write("window.BACKTESTS = ")
                json.dump(entries, f, default=float)
                f.write(";")
            os.replace(tmp, BTJS)
        print(f"stamped {stamped} of {len(entries)} entries")
        for k in sorted(classes, key=str):
            print(f"  {k}: {classes[k]}")


if __name__ == "__main__":
    main()
