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


def max_hold_days(trades):
    import datetime as dt
    mx = 0.0
    for t in trades or []:
        try:
            a = dt.datetime.strptime(t["entry_t"], "%Y-%m-%d %H:%M:%S")
            b = dt.datetime.strptime(t["exit_t"], "%Y-%m-%d %H:%M:%S")
            mx = max(mx, (b - a).total_seconds() / 86400.0)
        except Exception:
            pass
    return round(mx, 2)


def prune_entry(e, max_curve=500, max_trades=300):
    # stamp actual longest hold BEFORE trades get capped/stripped
    if e.get("max_hold_days") is None and e.get("trades"):
        e["max_hold_days"] = max_hold_days(e["trades"])
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
        json.dump(dict(curve=c, trades=t, config=e.get("config")), f)
    wk, mo = period_flags(c)
    e["wk_ok"], e["mo_ok"] = wk, mo
    e.pop("curve", None)      # curves+configs live in the detail file now —
    e.pop("config", None)     # they were 85% of a 177MB list file
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


def _week_key(dt_):
    import datetime as _dt
    day = dt_.weekday()                      # Mon=0 (matches JS (getUTCDay+6)%7)
    th = dt_ + _dt.timedelta(days=3 - day)   # Thursday of this week
    doy = (th - _dt.datetime(th.year, 1, 1)).days + 1
    return f"{th.year}-{-(-doy // 7)}"       # ceil(doy/7)


def period_flags(curve):
    """(wk_ok, mo_ok) computed from the FULL curve — ports the page's
    _noNegWeek/_noNegMonth so the list file doesn't need curves at all."""
    import datetime as _dt
    weeks, months, gapw = {}, {}, set()
    prev = None
    for p in curve or []:
        if not p or p.get("eq") is None or p.get("t") == "(data gap)":
            if prev:
                gapw.add(prev)
            prev = None
            continue
        try:
            d = _dt.datetime.strptime(str(p["t"])[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                d = _dt.datetime.strptime(str(p["t"])[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
        k = _week_key(d)
        weeks[k] = p["eq"]
        months[str(p["t"])[:7]] = p["eq"]
        if prev and prev != k and prev in gapw:
            gapw.add(k)
        prev = k
    ks = list(weeks)
    wk_ok = True
    for i in range(1, len(ks)):
        if ks[i] in gapw or ks[i - 1] in gapw:
            continue
        if weeks[ks[i]] < weeks[ks[i - 1]] - 1e-9:
            wk_ok = False
            break
    ms = list(months)
    mo_ok = all(months[ms[i]] >= months[ms[i - 1]] - 1e-9
                for i in range(1, len(ms)))
    return wk_ok, mo_ok


# ---------- optimizer-settings backfill ----------
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "optimizer", "runs")
_BT_SUFFIXES = ("_oosbest_full", "_oosbest", "_full", "_holdout",
                "_best_full", "_best")


def _opt_from_cfg(cfg):
    """Mirror of backtest_cli.opt_settings (kept standalone: importing
    backtest_cli would drag in numpy/pandas/_bootstrap for the merge loop)."""
    if not isinstance(cfg, dict) or ("algo" not in cfg and "evaluated" not in cfg):
        return None
    ho = (f"alternating {cfg['holdout_days']:g}d blocks" if cfg.get("holdout_days")
          else f"after {cfg['train_end']}" if cfg.get("train_end") else "none")
    return dict(algo=cfg.get("algo"),
                param_set=("per-regime" if cfg.get("per_regime", True)
                           else "single set"),
                holdout=ho, max_dd=cfg.get("max_dd"),
                max_hold_days=cfg.get("max_hold_days"),
                gap_mode=cfg.get("gap_mode"), lockbox=cfg.get("lockbox"),
                scoring=cfg.get("scoring"),
                anchor=cfg.get("anchor"),
                anchor_strength=cfg.get("anchor_strength"),
                evaluated=cfg.get("evaluated"))


def stamp_opt(e):
    """Backfill e['opt'] from the source run's best_config.json when the
    publisher didn't record it (older backtest_cli paths, oosbest configs)."""
    if e.get("opt") or not e.get("name"):
        return e
    nm = e["name"]
    for c in [nm] + [nm[:-len(sf)] for sf in _BT_SUFFIXES if nm.endswith(sf)]:
        p = os.path.join(RUNS_DIR, c, "best_config.json")
        if os.path.exists(p):
            try:
                o = _opt_from_cfg(json.load(open(p)))
                if o:
                    e["opt"] = o
            except Exception:
                pass
            break
    return e
