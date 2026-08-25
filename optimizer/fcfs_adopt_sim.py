#!/usr/bin/env python3
"""What-if replay for the SPOT FCFS router: same components, same one-slot
FCFS mechanics, plus SHADOW ADOPTION variants. Engine-exact component trades
(fcfsx collect outputs: [et_ns, xt_ns, r, mae]) + spot 1m closes for prices.

Variant knobs (a config dict per job):
  red         adopt when a shadow's unrealized <= red (e.g. -0.03)
  red_map     per-pair thresholds (overrides red), e.g. vol-scaled
  pairs       restrict adoption to these pairs (fresh signals unaffected)
  target      None = mirror the component's own exit; float = close at
              +target from OUR entry (virtual exit as fallback; same-trade
              re-adopt only 1% below our exit — the live anti-churn rule)
  prio        if set (e.g. -0.05): when a shadow is at/below this, it BEATS a
              simultaneous fresh signal for the slot
  swap        dict(pmin=..., depth=...): while holding a FRESH trade in
              profit >= pmin, close it early and adopt a shadow <= depth
              (the manual BTC->XRP move, automated); swapped-out trades are
              never re-adopted (live late-skip semantics)
Adopted round-trips pay 2x0.05% taker; engine trades keep their own costs.
Early swap exits use close-price returns (no extra fee, same as engine).
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
FEE2 = 0.001
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
    comps, prices = [], {}
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

    vols = {}
    for pair, (ts, px) in prices.items():
        r1 = np.diff(np.log(px[-260000:]))
        vols[pair] = float(np.std(r1) * np.sqrt(60) * 100)
    print("hourly vol %:", {k: round(v, 2) for k, v in
                            sorted(vols.items(), key=lambda x: -x[1])})

    def px_at(pair, t):
        ts, px = prices[pair]
        i = np.searchsorted(ts, t, side="right") - 1
        return px[i] if i >= 0 else None

    T0 = min(int(c["et"][0]) for c in comps)
    T1 = max(int(c["xt"][-1]) for c in comps)

    def simulate(cfg):
        red = cfg.get("red")
        red_map = cfg.get("red_map")
        pairs = cfg.get("pairs")
        target = cfg.get("target")
        prio = cfg.get("prio")
        swap = cfg.get("swap")
        adopt_on = red is not None or red_map is not None

        def red_for(pair):
            if red_map is not None:
                return red_map[pair]
            return red

        ptr = [0] * len(comps)
        slot = None
        last_exit = {}
        never = set()             # (ci,k) swapped out — live late-skip
        trades = []
        free_since = -1
        n_swap = 0
        t = T0

        def deepest_shadow(now):
            best = None
            for ci, c in enumerate(comps):
                if pairs is not None and c["pair"] not in pairs:
                    continue
                k = ptr[ci]
                if k >= len(c["et"]) or not (c["et"][k] <= now < c["xt"][k]):
                    continue
                if (ci, k) in never:
                    continue
                if slot is not None and slot["ci"] == ci and slot["k"] == k:
                    continue
                p = px_at(c["pair"], now)
                if p is None:
                    continue
                u = p / c["epx"][k] - 1.0
                if u > red_for(c["pair"]):
                    continue
                lx = last_exit.get((ci, k))
                if lx is not None and p > lx * 0.99:
                    continue
                if best is None or u < best[0]:
                    best = (u, ci, k, p)
            return best

        while t <= T1 + NS_MIN:
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
                        last_exit[(ci, slot["k"])] = vexit
                    trades.append((slot["t0"], slot["xt"], ret, trough,
                                   slot["kind"]))
                    free_since = slot["xt"]
                    slot = None
                    closed = True
                elif slot["kind"] == "adopt":
                    p = px_at(c["pair"], t)
                    if p is not None:
                        u = p / slot["apx"] - 1.0
                        slot["trough"] = min(slot["trough"], u)
                        if target is not None and u >= target:
                            trades.append((slot["t0"], t, target - FEE2,
                                           min(slot["trough"], 0.0), "adopt"))
                            last_exit[(ci, slot["k"])] = \
                                slot["apx"] * (1 + target)
                            free_since = t
                            slot = None
                            closed = True
                elif slot["kind"] == "fresh" and swap and adopt_on:
                    p = px_at(c["pair"], t)
                    if p is not None:
                        u_our = p / c["epx"][slot["k"]] - 1.0
                        if u_our >= swap["pmin"]:
                            deep = deepest_shadow(t)
                            if deep is not None and deep[0] <= swap["depth"]:
                                trades.append((slot["t0"], t, u_our,
                                               min(c["mae"][slot["k"]], 0.0),
                                               "swapout"))
                                never.add((ci, slot["k"]))
                                free_since = t
                                slot = None
                                closed = True
                                n_swap += 1
                if not closed and slot is not None:
                    t += NS_MIN
                    continue
            # ---- slot free ----
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
            deep = deepest_shadow(t) if adopt_on else None
            take_deep = (deep is not None
                         and (best_fresh is None
                              or (prio is not None and deep[0] <= prio)))
            if best_fresh is not None and not take_deep:
                et_, xt_, ci, k = best_fresh
                slot = dict(kind="fresh", ci=ci, k=k, t0=max(et_, t), xt=xt_)
                continue
            if take_deep:
                u, ci, k, p = deep
                slot = dict(kind="adopt", ci=ci, k=k, t0=t,
                            xt=int(comps[ci]["xt"][k]), apx=p, trough=u)
                continue
            t += NS_MIN

        eq, peak, mdd = 1000.0, 1000.0, 0.0
        wins, mo, n_ad, ad_ret = 0, {}, 0, 0.0
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
        return dict(n=len(trades), n_adopt=n_ad, n_swap=n_swap,
                    adopt_contrib_pct=100 * (math.exp(ad_ret) - 1),
                    total_mult=eq / 1000.0,
                    monthly=100 * ((max(eq, 1e-9) / 1000.0)
                                   ** (1 / months) - 1),
                    dd=100 * mdd, win=100 * wins / max(len(trades), 1),
                    months=months,
                    neg_months=sum(1 for v in mo.values() if v < 1.0))

    hype_vol = vols["hype"]
    vol_map = {p: max(-0.08, -0.03 * hype_vol / v) for p, v in vols.items()}
    jobs = [
        ("BASELINE (pure FCFS)", {}),
        ("best known: HYPE-only <=-3%, mirror exit",
         dict(red=-0.03, pairs={"hype"})),
        ("vol-scaled thresholds, all pairs, mirror exit "
         + str({k: round(100 * v, 1) for k, v in vol_map.items()}),
         dict(red_map=vol_map)),
        ("HYPE <=-3% mirror + PRIORITY over fresh when <=-5%",
         dict(red=-0.03, pairs={"hype"}, prio=-0.05)),
        ("HYPE <=-3% mirror + SWAP-OUT (fresh in profit >=+0.3% yields to "
         "shadow <=-4%)",
         dict(red=-0.03, pairs={"hype"}, swap=dict(pmin=0.003, depth=-0.04))),
        ("HYPE <=-3%, close at +3% (virtual exit fallback)",
         dict(red=-0.03, pairs={"hype"}, target=0.03)),
        ("HYPE <=-3%, close at +5% (virtual exit fallback)",
         dict(red=-0.03, pairs={"hype"}, target=0.05)),
        ("COMBO: vol-scaled thresholds + close at +3%",
         dict(red_map=vol_map, target=0.03)),
        ("COMBO: vol-scaled thresholds + close at +2%",
         dict(red_map=vol_map, target=0.02)),
    ]
    if len(sys.argv) > 1 and sys.argv[1] == "--combo":
        jobs = [jobs[0]] + jobs[-2:]
    for label, cfg in jobs:
        s = simulate(cfg)
        print(f"\n{label}")
        print(f"  {s['n']} trades ({s['n_adopt']} adopted"
              + (f", {s['n_swap']} swap-outs" if s['n_swap'] else "")
              + f") | {s['monthly']:+.2f}%/mo | total x{s['total_mult']:.2f}"
              f" | maxDD {s['dd']:.1f}% | win {s['win']:.0f}%"
              f" | losing months {s['neg_months']}")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
