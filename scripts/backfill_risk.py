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


DETAIL = os.path.join(REPO, "dashboard", "bt_detail")


def _with_config(e):
    """risk_of needs `config`, but entries are slimmed AT INGEST — the config
    lives in bt_detail/<name>.json and the list entry keeps only scalars. So a
    plain risk_of() over the store cannot classify anything already slimmed:
    the first backfill pass left 640 entries unstamped, 378 of the first 400
    of which had their config sitting in bt_detail all along. Borrow it for
    the classification only; the entry itself stays slim.
    """
    if e.get("config"):
        return e
    p = os.path.join(DETAIL, f"{e.get('name')}.json")
    if not os.path.exists(p):
        return e
    try:
        d = json.load(open(p))
    except Exception:
        return e
    if not isinstance(d, dict) or not d.get("config"):
        return e
    merged = dict(e)
    merged["config"] = d["config"]
    if not merged.get("stats") and d.get("stats"):
        merged["stats"] = d["stats"]
    return merged


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
                lv, slc = risk_of(_with_config(e))
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
