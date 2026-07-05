#!/usr/bin/env python3
"""Parity: live StrategyV7 vs engine3 on the last data segment using the best
regime-specialist study config. Should print PARITY: PASS."""
import os, sys, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))
os.chdir(os.path.join(HERE, "research"))

from strategy_v7 import StrategyV7
from engine3 import precompute3, run3, vec3
from regimes import make_regimes


def main():
    cfg_path = os.path.join(HERE, "..", "optimizer", "runs",
                            "study_lev_vol3_gen", "best_config.json")
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

    pre = precompute3(df3, df1)
    regs, R = make_regimes(pre["feats"], fc["method"])
    P = np.vstack([vec3(reg) for reg in fc["cand"]["regs"]])
    if P.shape[0] != R:
        P = np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
    use_sl = fc["mode"] == "spot"
    ref, eq, liq = run3(pre, P, regime=regs, warmup=3000, use_sl=use_sl,
                        dyn_liq=(fc["mode"] == "lev"))
    print(f"engine3: {len(ref)} trades, eq {eq:.0f}, liq {liq}")

    strat = StrategyV7(cfg, {})
    live = []
    pos_open = False
    n = len(pre["c"])
    for i in range(3000, n):
        acts = strat.decide_at(pre, regs, P, i)
        for a in acts:
            if a["do"] == "open" and not pos_open:
                pos_open = True
                strat.state["position"] = dict(dir=a["dir"], system=a["system"],
                                               regime=a["regime"], lev=a["lev"],
                                               entry_price=pre["o"][i + 1] if i + 1 < n else a["ref_close"],
                                               sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"])
                live.append((pd.Timestamp(pre["t"][i + 1]) if i + 1 < n else None, a["dir"]))
            elif a["do"] == "close" and pos_open:
                pos_open = False
                strat.state["position"] = None
    ref_set = set(zip(pd.to_datetime(ref["entry_t"]), ref["dir"].astype(int)))
    live_set = set((t, d) for t, d in live if t is not None)
    both = ref_set & live_set
    print(f"live: {len(live_set)} | engine3: {len(ref_set)} | match: {len(both)}")
    ok = len(both) >= 0.97 * max(len(ref_set), len(live_set), 1)
    print("PARITY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
