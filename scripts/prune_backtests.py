#!/usr/bin/env python3
"""Prune display copies of backtest entries so backtests.js stays loadable.
Curves are evenly downsampled (first+last kept), trades capped to the most
recent. Full-fidelity payloads remain in runs/<run>/bts/ on the box.
Usage: python3 prune_backtests.py [--max-curve 500] [--max-trades 300]
"""
import argparse, fcntl, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "..", "dashboard", "backtests.js")


DETAIL_DIR = os.path.join(HERE, "..", "dashboard", "bt_detail")


def prune_entry(e, max_curve=500, max_trades=300):
    c = e.get("curve") or []
    if len(c) > max_curve:
        step = (len(c) - 1) / (max_curve - 1)
        e["curve"] = [c[round(i * step)] for i in range(max_curve - 1)] + [c[-1]]
        e["curve_pruned_from"] = len(c)
    t = e.get("trades") or []
    if len(t) > max_trades:
        e["trades"] = t[-max_trades:]
        e["trades_pruned_from"] = len(t)
    return e


def split_entry(e, list_curve=200):
    """Move curve+trades to dashboard/bt_detail/<name>.json (fetched on row
    click); keep only a coarse curve in the list for filters/sparkline."""
    if not e.get("name"):
        return e
    c = e.get("curve") or []
    t = e.get("trades") or []
    if not c and not t:
        return e
    os.makedirs(DETAIL_DIR, exist_ok=True)
    with open(os.path.join(DETAIL_DIR, e["name"] + ".json"), "w") as f:
        json.dump(dict(curve=c, trades=t), f)
    if len(c) > list_curve:
        step = (len(c) - 1) / (list_curve - 1)
        e["curve"] = [c[round(i * step)] for i in range(list_curve - 1)] + [c[-1]]
    e["trades"] = []
    e["detail"] = True
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-curve", type=int, default=500)
    ap.add_argument("--max-trades", type=int, default=300)
    a = ap.parse_args()
    with open(P + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(P).read()
        entries = json.JSONDecoder().raw_decode(
            txt[txt.index("=") + 1:].lstrip())[0]
        del txt
        n = 0
        for e in entries:
            if e.get("detail"):
                continue
            prune_entry(e, a.max_curve, a.max_trades)
            split_entry(e)
            n += 1
        tmp = P + ".tmpprune"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS = ")
            json.dump(entries, f)
            f.write(";")
        os.replace(tmp, P)
    print(f"pruned {n} of {len(entries)} entries; "
          f"{os.path.getsize(P)//1048576} MB now")


if __name__ == "__main__":
    main()
