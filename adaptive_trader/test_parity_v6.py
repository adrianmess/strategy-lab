#!/usr/bin/env python3
"""Parity: live StrategyV6 evaluator vs the numba engine on the last segment,
using the production candidate config. Should print PARITY: PASS."""
import os, sys, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))
os.chdir(os.path.join(HERE, "research"))   # caches land here

from strategy_v6 import StrategyV6
from fast_engine import precompute, run_fast
from adaptive import make_adaptive_pre
from regimes import make_regimes
from wf2 import build_P_v6, TREND_VARIANTS, FUT_COMM, SPOT_COMM


def main():
    cfg_path = os.path.join(HERE, "research2", "final_config_v6_lev_none.json")
    fc = json.load(open(cfg_path))
    cfg = dict(candidate=fc["cand"], mode=fc["mode"], method=fc["method"])

    df3 = pd.read_parquet("data/sol_3min.parquet")
    df1 = pd.read_parquet("data/sol_1min.parquet")
    df3["t"] = df3["t"].dt.tz_localize(None)
    df1["t"] = df1["t"].dt.tz_localize(None)
    gaps = df3["t"].diff().dt.total_seconds().div(60)
    seg_start = df3.index[gaps > 1000].max()
    df3 = df3.loc[seg_start:].reset_index(drop=True)
    df1 = df1[df1["t"] >= df3["t"].iloc[0]].reset_index(drop=True)

    pre = precompute(df3, df1)
    q, f = make_adaptive_pre(pre, trend_block_z=TREND_VARIANTS[fc["cand"].get("tv", 0)])
    regs, R = make_regimes(f, fc["method"])
    P = build_P_v6(fc["cand"], R)
    use_sl = fc["mode"] == "spot"
    ref, eq, liq = run_fast(q, P, regime=regs, warmup=3000, use_sl=use_sl,
                            commission=FUT_COMM if fc["mode"] == "lev" else SPOT_COMM,
                            liq_threshold=-1.0 if fc["mode"] == "lev" else 1e9)
    print(f"engine: {len(ref)} trades, eq {eq:.0f}, liq {liq}")

    strat = StrategyV6(cfg, {})
    live = []
    pos_open = False
    n = len(q["c"])
    for i in range(3000, n):
        acts = strat.decide_at(q, regs, R, i)
        for a in acts:
            if a["do"] == "open" and not pos_open:
                pos_open = True
                strat.state["position"] = dict(dir=a["dir"], system=a["system"],
                                               regime=a["regime"], lev=a["lev"],
                                               entry_price=q["o"][i + 1] if i + 1 < n else a["ref_close"],
                                               sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"])
                live.append((pd.Timestamp(q["t"][i + 1]) if i + 1 < n else None, a["dir"]))
            elif a["do"] == "close" and pos_open:
                pos_open = False
                strat.state["position"] = None
    ref_set = set(zip(pd.to_datetime(ref["entry_t"]), ref["dir"].astype(int)))
    live_set = set((t, d) for t, d in live if t is not None)
    both = ref_set & live_set
    print(f"live: {len(live_set)} | engine: {len(ref_set)} | match: {len(both)}")
    ok = len(both) >= 0.97 * max(len(ref_set), len(live_set), 1)
    print("PARITY:", "PASS" if ok else "FAIL")
    if not ok:
        print("engine-only:", sorted(ref_set - live_set)[:5])
        print("live-only:", sorted(live_set - ref_set)[:5])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
