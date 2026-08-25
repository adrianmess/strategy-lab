#!/usr/bin/env python3
"""What-if replay for the SPOT FCFS router: same components, same one-slot
FCFS baseline, PLUS shadow adoption — when the slot is free and some
component's open virtual trade is >= RED underwater, adopt it at the live
price. Two exit variants:
  A  mirror: close where the component's own trade closes (exit px derived
     exactly: entry_px * (1 + r) from the engine's return)
  B  target: close at +TGT from OUR adopt price (virtual exit as fallback);
     the same virtual trade re-adopts only 1% below our last exit (the live
     anti-churn rule)
Fresh FCFS signals behave identically in all variants and keep priority.
Adopted round-trips pay 2x0.05% taker; engine trades keep their own costs.
Uses the fcfsx collect outputs (_t_*.json: [et_ns, xt_ns, r, mae]) plus the
pairs' spot 1m closes for prices.
"""
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "runs", "All_pairs_SPOT_1m-3m_4Dmax_multi-strat")
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
RED = -0.01          # adopt when unreal <= -1%
TGT = 0.01           # variant B: close at +1% from OUR entry
FEE2 = 0.001         # taker both sides on adopted round-trips
NS_MIN = 60_000_000_000


def load_prices(pair):
    df = pd.read_parquet(os.path.join(DATA, f"{pair}_spot_1min.parquet"))
    tc = [c for c in df.columns
          if c.lower() in ("t", "time", "ts", "datetime", "date")][0]
    df[tc] = pd.to_datetime(df[tc])
    s = df.set_index(tc)["close"]
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    return s.index.asi8, s.values.astype(np.float64)


def main():
    comps = []            # dicts: run, pair, et[], xt[], r[], mae[], epx[]
    prices = {}
    for f in sorted(glob.glob(os.path.join(RUN, "_t_*.json"))):
        pair = os.path.basename(f).split("_")[2]
        if pair not in prices:
            prices[pair] = load_prices(pair)
        ts, px = prices[pair]
        for run, tab in json.load(open(f)).items():
            tr = tab.get("trades") or []
            if not tr:
                continue
            et = np.array([x[0] for x in tr], dtype=np.int64)
            xt = np.array([x[1] for x in tr], dtype=np.int64)
            r = np.array([x[2] for x in tr])
            mae = np.array([x[3] for x in tr])
            idx = np.clip(np.searchsorted(ts, et, side="right") - 1, 0, None)
            comps.append(dict(run=run, pair=pair, et=et, xt=xt, r=r, mae=mae,
                              epx=px[idx]))
    print(f"{len(comps)} components loaded")

    def px_at(pair, t):
        ts, px = prices[pair]
        i = np.searchsorted(ts, t, side="right") - 1
        return px[i] if i >= 0 else None

    T0 = min(int(c["et"][0]) for c in comps)
    T1 = max(int(c["xt"][-1]) for c in comps)

    def simulate(variant, red=RED):
        """variant: None (baseline) | 'A' (mirror exit) | 'B' (+1% target)."""
        ptr = [0] * len(comps)
        slot = None
        last_exit = {}          # (ci,k) -> our exit px (B churn guard)
        trades = []             # (et, xt, ret, trough_frac, kind)
        free_since = -1         # fcfs_merge parity: a fresh trade is taken
        t = T0                  # only if it ENTERED at/after the slot freed
        while t <= T1 + NS_MIN:
            # component trade-pointer upkeep happens implicitly via slot/adopt
            if slot is not None:
                ci = slot["ci"]
                c = comps[ci]
                closed = False
                if t >= slot["xt"]:
                    if slot["kind"] == "fresh":
                        ret = c["r"][slot["k"]]
                        trough = min(c["mae"][slot["k"]], 0.0)
                    else:
                        vexit = c["epx"][slot["k"]] * (1.0 + c["r"][slot["k"]])
                        ret = vexit / slot["apx"] - 1.0 - FEE2
                        trough = min(slot["trough"], 0.0)
                    trades.append((slot["t0"], slot["xt"], ret, trough,
                                   slot["kind"]))
                    if slot["kind"] == "adopt":
                        last_exit[(ci, slot["k"])] = vexit
                    free_since = slot["xt"]
                    slot = None
                    closed = True
                elif slot["kind"] == "adopt":
                    p = px_at(c["pair"], t)
                    if p is not None:
                        u = p / slot["apx"] - 1.0
                        slot["trough"] = min(slot["trough"], u)
                        if variant == "B" and u >= TGT:
                            ret = TGT - FEE2
                            trades.append((slot["t0"], t, ret,
                                           min(slot["trough"], 0.0), "adopt"))
                            last_exit[(ci, slot["k"])] = slot["apx"] * (1 + TGT)
                            free_since = t
                            slot = None
                            closed = True
                if not closed and slot is not None:
                    t += NS_MIN
                    continue
            # slot is free: fresh signals first (FCFS priority), then adoption
            best_fresh = None
            for ci, c in enumerate(comps):
                k = ptr[ci]
                n = len(c["et"])
                while k < n and c["xt"][k] <= t:
                    k += 1
                ptr[ci] = k
                if k < n and free_since <= c["et"][k] <= t:
                    cand = (c["et"][k], c["xt"][k], ci, k)
                    if best_fresh is None or cand < best_fresh:
                        best_fresh = cand
            if best_fresh is not None:
                et_, xt_, ci, k = best_fresh
                slot = dict(kind="fresh", ci=ci, k=k, t0=max(et_, t), xt=xt_)
                ptr[ci] = k + 1
                continue
            if variant in ("A", "B"):
                deepest = None
                for ci, c in enumerate(comps):
                    k = ptr[ci]
                    if k >= len(c["et"]) or not (c["et"][k] <= t < c["xt"][k]):
                        continue
                    p = px_at(c["pair"], t)
                    if p is None:
                        continue
                    u = p / c["epx"][k] - 1.0
                    if u > red:
                        continue
                    lx = last_exit.get((ci, k))
                    if lx is not None and p > lx * 0.99:
                        continue
                    if deepest is None or u < deepest[0]:
                        deepest = (u, ci, k, p)
                if deepest is not None:
                    u, ci, k, p = deepest
                    c = comps[ci]
                    slot = dict(kind="adopt", ci=ci, k=k, t0=t,
                                xt=int(c["xt"][k]), apx=p, trough=u)
                    continue
            t += NS_MIN

        # ---- stats (replay_stats-compatible) ----
        eq, peak, mdd = 1000.0, 1000.0, 0.0
        wins = 0
        mo = {}
        n_ad = 0
        ad_ret = 0.0
        for et_, xt_, ret, trough, kind in trades:
            ret = max(ret, -0.999)
            tr_eq = eq * (1.0 + trough)
            eq *= (1.0 + ret)
            peak = max(peak, eq)
            mdd = max(mdd, 1.0 - min(tr_eq, eq) / peak)
            wins += ret > 0
            m = str(np.datetime64(int(et_), "ns"))[:7]
            mo[m] = mo.get(m, 1.0) * (1.0 + ret)
            if kind == "adopt":
                n_ad += 1
                ad_ret += math.log(1 + ret)
        months = max(len(mo), 1)
        neg = sum(1 for v in mo.values() if v < 1.0)
        return dict(n=len(trades), n_adopt=n_ad,
                    adopt_contrib_pct=100 * (math.exp(ad_ret) - 1),
                    total_mult=eq / 1000.0,
                    monthly=100 * ((max(eq, 1e-9) / 1000.0) ** (1 / months) - 1),
                    dd=100 * mdd, win=100 * wins / max(len(trades), 1),
                    months=months, neg_months=neg,
                    tpm=len(trades) / months)

    jobs = [("BASELINE (pure FCFS)", None, RED)]
    if len(sys.argv) > 1 and sys.argv[1] == "--sweep":
        for rr in (-0.015, -0.02, -0.03, -0.04, -0.05):
            jobs.append((f"A: adopt <= {100*rr:g}%, mirror exit", "A", rr))
    else:
        jobs += [("A: adopt <= -1%, mirror the strategy exit", "A", RED),
                 ("B: adopt <= -1%, close at +1% (virtual exit fallback)",
                  "B", RED)]
    for label, v, rr in jobs:
        s = simulate(v, rr)
        print(f"\n{label}")
        print(f"  {s['n']} trades ({s['n_adopt']} adopted) over {s['months']}"
              f" months | {s['monthly']:+.2f}%/mo | total x{s['total_mult']:.2f}"
              f" | maxDD {s['dd']:.1f}% | win {s['win']:.0f}%"
              f" | {s['tpm']:.1f} trades/mo | losing months {s['neg_months']}")
        if s["n_adopt"]:
            print(f"  adopted trades alone compound to "
                  f"{s['adopt_contrib_pct']:+.1f}% total")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
