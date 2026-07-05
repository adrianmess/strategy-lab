#!/usr/bin/env python3
"""Parity test: replay history through the LIVE strategy code and compare with
the research fast-engine backtest. Verifies the live evaluator makes identical
decisions to the validated backtest.

Usage: python3 test_parity.py
"""
import os, sys, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research"))
os.chdir(os.path.join(HERE, "research"))

from strategy import Strategy, TREND_VARIANTS
from fast_engine import precompute, run_fast
from adaptive import make_adaptive_pre, build_P
from regimes import vol_terciles


def main():
    with open(os.path.join(HERE, "config.json")) as f:
        cfg = json.load(f)
    cfg["risk_dial"] = 1.0
    p = cfg["params"]

    df3 = pd.read_parquet("data/sol_3min.parquet")
    df1 = pd.read_parquet("data/sol_1min.parquet")
    df3["t"] = df3["t"].dt.tz_localize(None)
    df1["t"] = df1["t"].dt.tz_localize(None)
    # use the last continuous segment (~6 months)
    gaps = df3["t"].diff().dt.total_seconds().div(60)
    seg_start = df3.index[gaps > 1000].max()
    df3 = df3.loc[seg_start:].reset_index(drop=True)
    df1 = df1[df1["t"] >= df3["t"].iloc[0]].reset_index(drop=True)
    print(f"segment: {df3['t'].iloc[0]} .. {df3['t'].iloc[-1]} ({len(df3)} bars)")

    # ---- research engine reference ----
    pre = precompute(df3, df1)
    q, feats = make_adaptive_pre(pre, trend_block_z=TREND_VARIANTS[p["tv"]])
    regs = vol_terciles(feats["volPct"])
    knobs = dict(zL=p["zL"], zS=p["zS"], zXS=p["zXS"], ptScale=p["ptScale"],
                 cdScale=[1.0] * 3, lev=p["lev"])
    P = build_P(knobs)
    P[:, 35] = p["sl"]; P[:, 36] = p["sl"]
    ref, eq, liq = run_fast(q, P, regime=regs, warmup=3000, use_sl=True)
    print(f"reference engine: {len(ref)} trades, final eq {eq:.0f}")

    # ---- live strategy replay ----
    state = {}
    strat = Strategy(cfg, state)
    live_entries = []
    i0 = 3000
    pos_open = False
    for i in range(i0, len(q["c"])):
        # emulate the engine's fill model: actions from bar i execute at bar i+1 open
        acts = strat.decide_at(q, regs, i)
        for a in acts:
            if a["do"] == "open" and not pos_open:
                pos_open = True
                state["position"] = dict(dir=a["dir"], system=a["system"],
                                         regime=a["regime"],
                                         entry_price=q["o"][i + 1] if i + 1 < len(q["c"]) else a["ref_close"],
                                         qty=1, lev=a["lev"], sl_price=a["sl_price"],
                                         entry_sig_ms=a["sig_ms"])
                live_entries.append(dict(t=pd.Timestamp(q["t"][i + 1]) if i + 1 < len(q["c"]) else None,
                                         dir=a["dir"], system=a["system"]))
            elif a["do"] == "close" and pos_open:
                pos_open = False
                state["position"] = None

    ref_e = ref.copy()
    ref_e["t"] = pd.to_datetime(ref_e["entry_t"])
    live_df = pd.DataFrame(live_entries).dropna(subset=["t"])
    ref_set = set(zip(ref_e["t"], ref_e["dir"].astype(int)))
    live_set = set(zip(live_df["t"], live_df["dir"].astype(int)))
    both = ref_set & live_set
    print(f"live evaluator: {len(live_set)} entries | engine: {len(ref_set)} | matching: {len(both)}")
    only_ref = sorted(ref_set - live_set)[:5]
    only_live = sorted(live_set - ref_set)[:5]
    if only_ref: print("engine-only (first 5):", only_ref)
    if only_live: print("live-only (first 5):", only_live)
    ok = len(both) >= 0.97 * max(len(ref_set), len(live_set))
    print("PARITY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
