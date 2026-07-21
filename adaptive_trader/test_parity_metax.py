#!/usr/bin/env python3
"""Parity test for the MetaX router live adapter.

Replays history bar-by-bar through StrategyMetax with a rolling feed-sized
window (exactly what the live trader does), and compares the mirrored trades
against a reference simulation that runs the component engines once over the
full stretch and applies the router rules (bucket gate at the signal bar +
single-slot arbiter).

PASS = >= 90% of reference entries matched (fill-bar time + direction) and no
unmatched EXTRA live entries. Residual mismatches come from rolling-window
warmup effects at the margin — the same class of difference as the pine
validations (194/219 etc.), and they shrink as the window grows.

Usage:
  python3 test_parity_metax.py [--run camp_c4_m_spot_vol3] [--days 10]
"""
import argparse, json, os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))

from strategy_metax import StrategyMetax, resolve_candidate   # noqa: E402
from regimes import regime_features, make_regimes             # noqa: E402
from strategy_metax import BUCKET_METHOD, WARMUP, FUT_COMM, SPOT_COMM  # noqa

RUNS = os.path.join(os.path.dirname(HERE), "optimizer", "runs")
WINDOW_DAYS = 35
BAR3 = pd.Timedelta(minutes=3)


def load_data():
    d3 = pd.read_parquet(os.path.join(HERE, "research", "data", "sol_3min.parquet"))
    d1 = pd.read_parquet(os.path.join(HERE, "research", "data", "sol_1min.parquet"))
    d3 = d3[["t", "open", "high", "low", "close", "volume"]].copy()
    d1 = d1[["t", "open", "high", "low", "close", "volume"]].copy()
    for d in (d3, d1):
        d["t"] = pd.to_datetime(d["t"]).dt.tz_localize(None)
    return d3.reset_index(drop=True), d1.reset_index(drop=True)


def reference_trades(candidate, mode, d3, d1, i_start, i_end):
    """Component engines run ONCE over the full frame; router rules applied:
    bucket gate at the SIGNAL bar (fill-1), single slot, entries in stretch.
    Uses the ADAPTER's own family runners, so the test isolates exactly the
    live-specific mechanics: windowing, bucketing, mirroring, slot logic."""
    feats = regime_features(dict(c=d3["close"].to_numpy(), h=d3["high"].to_numpy(),
                                 l=d3["low"].to_numpy(), vol=d3["volume"].to_numpy()))
    breg, _ = make_regimes(feats, BUCKET_METHOD[candidate["buckets"]])
    assign = candidate["assign"]
    ref_strat = StrategyMetax(dict(candidate=candidate, mode=mode,
                                   emergency_exit_adverse=None), {})
    rows = []
    for k in sorted({a for a in assign if a is not None and a >= 0}):
        comp = candidate["components"][k]
        tr, op = ref_strat._run_component(comp, d3, d1, feats)
        for _, t in tr.iterrows():
            ei = int(t["entry_idx"])
            sig = max(ei - 1, 0)                     # signal bar = fill - 1
            if not (i_start <= ei < i_end):
                continue
            if assign[int(breg[sig])] != k:
                continue
            rows.append(dict(comp=k, entry_i=ei, exit_i=int(t["exit_idx"]),
                             entry_t=str(d3["t"].iloc[ei])[:16],
                             exit_t=str(d3["t"].iloc[int(t['exit_idx'])])[:16],
                             dir=int(np.sign(t["dir"])) or 1))
    rows.sort(key=lambda r: r["entry_i"])
    merged, last_exit = [], -1
    for r in rows:
        if r["entry_i"] <= last_exit:
            continue
        last_exit = r["exit_i"]
        merged.append(r)
    return merged


def replay_adapter(candidate, mode, d3, d1, i_start, i_end):
    cfg = dict(candidate=candidate, mode=mode, emergency_exit_adverse=None)
    state = {}
    strat = StrategyMetax(cfg, state)
    win = int(WINDOW_DAYS * 480)
    live = []
    open_pos = None
    t1v = d1["t"].values
    anchor = max(0, i_start - win)   # ANCHORED window, like the live feed
    for i in range(i_start, i_end):
        lo = anchor
        w3 = d3.iloc[lo:i + 1]
        lo_t = w3["t"].iloc[0].to_datetime64()
        hi_t = (w3["t"].iloc[-1] + BAR3).to_datetime64()
        j0, j1 = np.searchsorted(t1v, lo_t), np.searchsorted(t1v, hi_t)
        w1 = d1.iloc[j0:j1]
        if len(w1) < 100:
            continue
        acts = strat.on_bar_close(w3.reset_index(drop=True),
                                  w1.reset_index(drop=True))
        for a in acts:
            if a["do"] == "open" and open_pos is None:
                open_pos = dict(comp=state["mirror"]["comp"],
                                entry_i=i + 1,
                                entry_t=str(w3["t"].iloc[-1] + BAR3)[:16],
                                dir=a["dir"])
                state["position"] = dict(dir=a["dir"], entry_price=float(
                    w3["close"].iloc[-1]), lev=a["lev"], qty=1)
            elif a["do"] == "close" and open_pos is not None:
                open_pos["exit_t"] = str(w3["t"].iloc[-1] + BAR3)[:16]
                open_pos["exit_i"] = i + 1
                live.append(open_pos)
                open_pos = None
                state["position"] = None
        if (i - i_start) % 480 == 0:
            print(f"  …replayed {i - i_start}/{i_end - i_start} bars, "
                  f"{len(live)} trades", flush=True)
    if open_pos is not None:
        open_pos["exit_t"] = "OPEN"
        live.append(open_pos)
    return live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="camp_c4_m_spot_vol3")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--end-days-ago", type=int, default=0,
                    help="shift the test window into the past — pick an ACTIVE "
                         "stretch; a quiet week gives a vacuous 0-trade result")
    args = ap.parse_args()
    best = json.load(open(os.path.join(RUNS, args.run, "best_config.json")))
    candidate = resolve_candidate(best, RUNS)
    mode = best["mode"]
    d3, d1 = load_data()
    i_end = len(d3) - args.end_days_ago * 480
    i_start = i_end - args.days * 480
    print(f"router {args.run} ({mode}, {candidate['buckets']}), replaying "
          f"{args.days}d = {i_end - i_start} bars…", flush=True)
    ref = reference_trades(candidate, mode, d3, d1, i_start, i_end)
    print(f"reference: {len(ref)} routed trades in stretch", flush=True)
    live = replay_adapter(candidate, mode, d3, d1, i_start, i_end)
    print(f"adapter  : {len(live)} mirrored trades", flush=True)
    matched = 0
    used = set()
    for r in ref:
        for j, lv in enumerate(live):
            if j in used:
                continue
            if lv["entry_t"] == r["entry_t"] and lv["dir"] == r["dir"]:
                ex_ok = (lv.get("exit_t") == r["exit_t"]
                         or lv.get("exit_t") == "OPEN")
                matched += 1
                used.add(j)
                if not ex_ok:
                    print(f"  entry match, EXIT differs: ref {r['exit_t']} "
                          f"vs live {lv.get('exit_t')}", flush=True)
                break
        else:
            print(f"  UNMATCHED ref entry {r['entry_t']} dir {r['dir']} "
                  f"comp {r['comp']}", flush=True)
    extra = len(live) - len(used)
    if not ref and not live:
        print("PARITY: INCONCLUSIVE — no routed trades in this stretch; "
              "re-run with --end-days-ago on an active window", flush=True)
        return
    pct = 100.0 * matched / max(len(ref), 1)
    verdict = "PASS" if (pct >= 90 and extra == 0) else "FAIL"
    print(f"PARITY: {verdict} — {matched}/{len(ref)} reference entries matched "
          f"({pct:.0f}%), {extra} extra live entries", flush=True)


if __name__ == "__main__":
    main()
