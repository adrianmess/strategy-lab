#!/usr/bin/env python3
"""RESEARCH: the ANCHORED DIP-BUYER — a companion instance whose entire
reference is the OTHER instance's open position.

Rule under test (Adrian's, exactly):
  * instance A (e.g. MEX2 Spot) holds a position, entered at price E
  * while A is holding, if price falls to E*(1-k)  (A is -k% underwater),
    the companion BUYS
  * the companion SELLS when price returns to E   (A is back to 0%)
  * repeat for as long as A holds the position

Variants swept:
  seq  — one companion position at a time, re-entering after each exit
  grid — stack up to N positions, each a further k% step down, all exiting
         at E (this is what "it falls again, open another" can also mean)

What decides it: the companion is only allowed to trade while A holds, and
its exit is A's breakeven, so it lives or dies on how often price dips k%
below A's entry and comes BACK. The killer risk is the tail: A's trade ends
(target hit or strategy exit) while the companion is still underwater. Those
are settled at the window's last price and reported separately as
"stranded" — that is where this kind of idea usually loses its money.

Rails: 1m bars, entry/exit at the trigger levels, taker fees both sides,
TRAIN/HOLDOUT split, and a CONTROL that runs the same rule anchored to
random reference prices outside A's hold windows.

  python3 anchored_dip_research.py --config <A's component config>
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "optimizer"))
DATA = os.path.join(REPO, "adaptive_trader", "research", "data")


def run_window(w, E, k, comm, mode, depth):
    """One of A's hold windows. Returns (closed trade returns, stranded
    returns, n_entries). Long-only: buy the dip, sell at E."""
    lo = w["low"].to_numpy()
    hi = w["high"].to_numpy()
    closed, open_pos = [], []          # open_pos: list of entry prices
    for l, h in zip(lo, hi):
        # exits first: any open slice whose target (E) is touched this bar
        if open_pos and h >= E:
            for ep in open_pos:
                closed.append((E / ep - 1.0) - 2 * comm)
            open_pos = []
        # entries: next step down still available?
        if mode == "seq":
            if not open_pos and l <= E * (1 - k):
                open_pos.append(E * (1 - k))
        else:                          # grid: -k, -2k, -3k ... up to depth
            while len(open_pos) < depth:
                nxt = E * (1 - k * (len(open_pos) + 1))
                if l <= nxt:
                    open_pos.append(nxt)
                else:
                    break
    stranded = []
    if open_pos:                       # A's trade ended while we were down
        last = float(w["close"].iloc[-1])
        for ep in open_pos:
            stranded.append((last / ep - 1.0) - 2 * comm)
    return closed, stranded, len(closed) + len(stranded)


def equity_run(holds, d1, E_of, k, comm, mode, depth, split):
    """Compound a companion ACCOUNT through the windows in time order.
    Each slice stakes equity/depth, so peak exposure is one full account.
    Returns (%/mo train, %/mo holdout, max drawdown, n trades, stranded)."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    marks = []                 # (timestamp, equity)
    n_cl = n_st = 0
    for (s, en, E) in sorted(holds, key=lambda x: x[0]):
        w = d1[(d1["t"] >= s) & (d1["t"] <= en)]
        if len(w) < 5:
            continue
        cl, st, _ = run_window(w, E, k, comm, mode, depth)
        for r in cl + st:
            eq += (eq / depth) * r
            peak = max(peak, eq)
            mdd = max(mdd, 1.0 - eq / peak)
        n_cl += len(cl)
        n_st += len(st)
        marks.append((en, eq))
    if not marks:
        return 0.0, 0.0, 0.0, 0, 0
    tr = [m for m in marks if m[0] <= split]
    ho = [m for m in marks if m[0] > split]

    def gm(seq, eq0):
        if not seq:
            return 0.0
        months = max((seq[-1][0] - seq[0][0]).days / 30.44, 1e-9)
        return 100 * ((seq[-1][1] / eq0) ** (1 / months) - 1)
    return (gm(tr, 1.0), gm(ho, tr[-1][1] if tr else 1.0), 100 * mdd,
            n_cl, n_st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--split", default="2026-01-01")
    ap.add_argument("--ks", default="0.005,0.01,0.015,0.02,0.03")
    ap.add_argument("--depth", type=int, default=3)
    a = ap.parse_args()

    import backtest_cli as BT
    cfg = json.load(open(a.config))
    pair, mode = cfg.get("pair", "SOL_USDT"), cfg.get("mode", "spot")
    comm = 0.0004 if mode == "lev" else 0.0005
    e = BT.run_single(a.config)
    holds = []
    for t in e["trades"]:
        try:
            if t.get("dir", "long") != "long":
                continue               # spot core: long only
            holds.append((pd.Timestamp(t["entry_t"]), pd.Timestamp(t["exit_t"]),
                          float(t["entry"])))
        except Exception:
            pass
    # the still-open position at the end of data counts too
    for op in (e.get("open_positions") or []):
        try:
            holds.append((pd.Timestamp(op["entry_t"]), pd.Timestamp(op["as_of"]),
                          float(op["entry"])))
        except Exception:
            pass

    coin = pair.split("_")[0].lower()
    suffix = "_spot" if cfg.get("market_data") == "spot" else ""
    d1 = pd.read_parquet(os.path.join(DATA, f"{coin}{suffix}_1min.parquet"))
    d1 = d1.rename(columns=str.lower)
    d1["t"] = pd.to_datetime(d1["t"]).dt.tz_localize(None)
    split = pd.Timestamp(a.split)

    print(f"ANCHORED DIP-BUYER on {pair} ({mode}), anchored to "
          f"{os.path.basename(os.path.dirname(a.config))}")
    print(f"{len(holds)} hold windows | core %/mo "
          f"{e['stats']['monthly_growth_pct']:+.1f} | fees {100*comm:.2f}%/side")
    print("\n'stranded' = companion still underwater when the core trade "
          "ended (settled at the window's last price)\n")
    print(f"{'mode':>5} {'k':>6} | {'TRAIN closed':>13} {'win':>5} "
          f"{'stranded':>9} {'net/window':>11} | {'HOLD closed':>12} "
          f"{'win':>5} {'stranded':>9} {'net/window':>11}")

    for mname, depth in (("seq", 1), ("grid", a.depth)):
        for k in [float(x) for x in a.ks.split(",")]:
            agg = {}
            for lbl, sel in (("tr", lambda en: en <= split),
                             ("ho", lambda en: en > split)):
                cl, st, nwin = [], [], 0
                for (s, en, E) in holds:
                    if not sel(en):
                        continue
                    w = d1[(d1["t"] >= s) & (d1["t"] <= en)]
                    if len(w) < 5:
                        continue
                    nwin += 1
                    c, sd, _ = run_window(w, E, k, comm, mname, depth)
                    cl += c
                    st += sd
                net = (sum(cl) + sum(st)) / max(nwin, 1)
                agg[lbl] = (len(cl),
                            100 * float(np.mean([x > 0 for x in cl])) if cl else 0,
                            len(st),
                            100 * float(np.mean(st)) if st else 0.0,
                            100 * net)
            t_, h_ = agg["tr"], agg["ho"]
            print(f"{mname:>5} {100*k:>5.1f}% | {t_[0]:>7} trades {t_[1]:>4.0f}% "
                  f"{t_[2]:>4} @{t_[3]:>+6.1f}% {t_[4]:>+10.3f}% | "
                  f"{h_[0]:>6} trades {h_[1]:>4.0f}% {h_[2]:>4} @{h_[3]:>+6.1f}% "
                  f"{h_[4]:>+10.3f}%")

    # ---- the number that actually compares: a compounding ACCOUNT ----
    print("\ncompanion ACCOUNT (stake = equity/depth per slice, so peak "
          "exposure = 1x):\n")
    print(f"{'mode':>5} {'k':>6} | {'TRAIN %/mo':>11} {'HOLDOUT %/mo':>13} "
          f"{'maxDD':>7} {'closed':>7} {'stranded':>9} | {'CONTROL hold %/mo':>18}")
    # control: same rule, same window lengths, anchored to the price at the
    # START of windows placed OUTSIDE A's holds (does A's entry matter?)
    busy = [(s, en) for (s, en, _) in holds]
    ctrl = []
    for (s, en, _) in holds:
        cs = en + (en - s)                     # shift one window-length later
        ce = cs + (en - s)
        if any(bs < ce and cs < be for bs, be in busy):
            continue
        w = d1[(d1["t"] >= cs) & (d1["t"] <= ce)]
        if len(w) > 5:
            ctrl.append((cs, ce, float(w["close"].iloc[0])))
    for mname, depth in (("seq", 1), ("grid", a.depth)):
        for k in [float(x) for x in a.ks.split(",")]:
            tr, ho, mdd, ncl, nst = equity_run(holds, d1, None, k, comm,
                                               mname, depth, split)
            _, cho, _, _, _ = equity_run(ctrl, d1, None, k, comm, mname,
                                         depth, split)
            print(f"{mname:>5} {100*k:>5.1f}% | {tr:>+10.1f}% {ho:>+12.1f}% "
                  f"{mdd:>6.1f}% {ncl:>7} {nst:>9} | {cho:>+17.1f}%")


if __name__ == "__main__":
    main()
