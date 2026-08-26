#!/usr/bin/env python3
"""What-if replay for the LEV FCFS router (research only — mirrors
fcfs_adopt_sim.py, which answered this for the SPOT router on 2026-08-25).

Same one-slot FCFS mechanics + SHADOW ADOPTION variants, with the three
things leverage adds:
  * margin-basis unrealized: u = (p/epx - 1) * dir * lev  (what the live
    shadow rows and auto-adopt thresholds use on a lev instance)
  * liquidation of ADOPTED entries: adverse price move >= 1/lev - 0.8%
    maintenance buffer wipes the margin (ret = -1); engine trades already
    carry their own liq outcomes inside r
  * fees scale with leverage: round-trip = 2 x 0.04%/side x lev margin-basis
    (the same per-side rate the engine trades in these tables were built
    with, so adopted and fresh legs are costed identically)

Tables carry [et, xt, r, mae] only (margin-basis), so per-trade direction and
leverage are inferred from perp 1m closes: dir = sign agreement of r with the
price move, lev = component-median |r| / |price move|. The inference is
printed so bad fits are visible.

Usage:  python3 fcfs_adopt_sim_lev.py            # full variant grid
        python3 fcfs_adopt_sim_lev.py --quick    # baseline + headline rows
"""
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "runs", "All_pairs_LEV_1m-3m_multi-strat")
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
FEE_SIDE = 0.0004                 # engine-parity per-side rate (price-basis)
MAINT = 0.008                     # maintenance+fees buffer (matches optimize)
NS_MIN = 60_000_000_000


def load_prices(pair):
    df = pd.read_parquet(os.path.join(DATA, f"{pair}_1min.parquet"))
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
            ei = np.clip(np.searchsorted(ts, et, side="right") - 1, 0, None)
            xi = np.clip(np.searchsorted(ts, xt, side="right") - 1, 0, None)
            epx, xpx = px[ei], px[xi]
            pm = xpx / np.maximum(epx, 1e-12) - 1.0
            # ---- infer dir per trade + one lev per component ----
            dirs = np.where(r * pm >= 0, 1, -1)
            good = np.abs(pm) > 5e-4
            lev = (float(np.median(np.abs(r[good]) / np.abs(pm[good])))
                   if good.sum() >= 3 else 1.0)
            lev = float(np.clip(round(lev, 1), 1.0, 100.0))
            comps.append(dict(run=run, pair=pair, et=et, xt=xt, r=r, mae=mae,
                              epx=epx, xpx=xpx, dir=dirs, lev=lev))
    print(f"{len(comps)} components loaded")
    for c in comps:
        sh = int((c["dir"] < 0).sum())
        print(f"  {c['pair']:>5} {c['run'][:42]:<42} lev~{c['lev']:>5.1f}x "
              f"{len(c['r'])} trades ({sh} short)")

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
        red = cfg.get("red")                    # margin-basis, e.g. -0.05
        red_map = cfg.get("red_map")
        pairs = cfg.get("pairs")
        target = cfg.get("target")              # margin-basis take-profit
        adopt_on = red is not None or red_map is not None

        def red_for(pair):
            return red_map[pair] if red_map is not None else red

        ptr = [0] * len(comps)
        slot = None
        last_exit = {}
        trades = []
        free_since = -1
        n_liq_ad = 0
        t = T0

        def deepest_shadow(now):
            best = None
            for ci, c in enumerate(comps):
                if pairs is not None and c["pair"] not in pairs:
                    continue
                k = ptr[ci]
                if k >= len(c["et"]) or not (c["et"][k] <= now < c["xt"][k]):
                    continue
                if slot is not None and slot["ci"] == ci and slot["k"] == k:
                    continue
                p = px_at(c["pair"], now)
                if p is None:
                    continue
                d, lv = int(c["dir"][k]), c["lev"]
                u = (p / c["epx"][k] - 1.0) * d * lv       # margin-basis
                if u > red_for(c["pair"]):
                    continue
                lx = last_exit.get((ci, k))
                if lx is not None and (lx - p) * d < 0.01 * lx:
                    continue          # anti-churn: 1% beyond our last exit
                if best is None or u < best[0]:
                    best = (u, ci, k, p)
            return best

        while t <= T1 + NS_MIN:
            if slot is not None:
                ci = slot["ci"]
                c = comps[ci]
                closed = False
                if slot["kind"] == "adopt":
                    p = px_at(c["pair"], t)
                    if p is not None:
                        d, lv = slot["d"], slot["lv"]
                        upx = (p / slot["apx"] - 1.0) * d
                        um = upx * lv
                        slot["trough"] = min(slot["trough"], um)
                        if upx <= -(1.0 / lv - MAINT):     # liquidated
                            trades.append((slot["t0"], t, -1.0, -1.0,
                                           "adopt"))
                            last_exit[(ci, slot["k"])] = p
                            free_since = t
                            slot = None
                            closed = True
                            n_liq_ad += 1
                        elif target is not None and um >= target:
                            trades.append((slot["t0"], t,
                                           target - 2 * FEE_SIDE * lv,
                                           min(slot["trough"], 0.0), "adopt"))
                            last_exit[(ci, slot["k"])] = p
                            free_since = t
                            slot = None
                            closed = True
                if not closed and slot is not None and t >= slot["xt"]:
                    if slot["kind"] == "fresh":
                        ret = c["r"][slot["k"]]
                        trough = min(c["mae"][slot["k"]], 0.0)
                    else:
                        d, lv = slot["d"], slot["lv"]
                        vexit = c["xpx"][slot["k"]]
                        ret = ((vexit / slot["apx"] - 1.0) * d * lv
                               - 2 * FEE_SIDE * lv)
                        trough = min(slot["trough"], 0.0)
                        last_exit[(ci, slot["k"])] = vexit
                    trades.append((slot["t0"], slot["xt"],
                                   max(ret, -1.0), trough, slot["kind"]))
                    free_since = slot["xt"]
                    slot = None
                    closed = True
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
            if best_fresh is not None and deep is None:
                et_, xt_, ci, k = best_fresh
                slot = dict(kind="fresh", ci=ci, k=k, t0=max(et_, t), xt=xt_)
                continue
            if deep is not None and best_fresh is None:
                u, ci, k, p = deep
                slot = dict(kind="adopt", ci=ci, k=k, t0=t,
                            xt=int(comps[ci]["xt"][k]), apx=p, trough=u,
                            d=int(comps[ci]["dir"][k]), lv=comps[ci]["lev"])
                continue
            if deep is not None and best_fresh is not None:
                et_, xt_, ci, k = best_fresh     # fresh wins ties (parity
                slot = dict(kind="fresh", ci=ci, k=k, t0=max(et_, t), xt=xt_)
                continue                          # with the spot study)
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
                ad_ret += math.log(max(1 + ret, 1e-9))
        months = max(len(mo), 1)
        return dict(n=len(trades), n_adopt=n_ad, n_liq_ad=n_liq_ad,
                    adopt_contrib_pct=100 * (math.exp(ad_ret) - 1),
                    total_mult=eq / 1000.0,
                    monthly=100 * ((max(eq, 1e-9) / 1000.0)
                                   ** (1 / months) - 1),
                    dd=100 * mdd, win=100 * wins / max(len(trades), 1),
                    months=months,
                    neg_months=sum(1 for v in mo.values() if v < 1.0))

    hype_vol = vols.get("hype") or max(vols.values())
    vol_map = {p: max(-0.20, -0.05 * hype_vol / v) for p, v in vols.items()}
    vol_map3 = {p: 3 * v for p, v in vol_map.items()}
    jobs = [
        ("BASELINE (pure FCFS, fresh signals only)", {}),
        ("all pairs <=-3% margin, mirror exit", dict(red=-0.03)),
        ("all pairs <=-5% margin, mirror exit", dict(red=-0.05)),
        ("all pairs <=-10% margin, mirror exit", dict(red=-0.10)),
        ("all pairs <=-15% margin, mirror exit", dict(red=-0.15)),
        ("all pairs <=-25% margin, mirror exit", dict(red=-0.25)),
        ("vol-scaled " + str({k: round(100 * v, 1)
                              for k, v in vol_map.items()})
         + ", mirror exit", dict(red_map=vol_map)),
        ("vol-scaled x3 " + str({k: round(100 * v, 1)
                                 for k, v in vol_map3.items()})
         + ", mirror exit", dict(red_map=vol_map3)),
        ("all pairs <=-10%, close at +5% margin",
         dict(red=-0.10, target=0.05)),
        ("all pairs <=-10%, close at +10% margin",
         dict(red=-0.10, target=0.10)),
        ("vol-scaled, close at +5% margin",
         dict(red_map=vol_map, target=0.05)),
        ("vol-scaled x3, close at +5% margin",
         dict(red_map=vol_map3, target=0.05)),
        ("vol-scaled x3, close at +10% margin",
         dict(red_map=vol_map3, target=0.10)),
    ]
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        jobs = jobs[:3]
    for label, cfg in jobs:
        s = simulate(cfg)
        print(f"\n{label}")
        print(f"  {s['n']} trades ({s['n_adopt']} adopted, "
              f"{s['n_liq_ad']} adopted-liq) | {s['monthly']:+.2f}%/mo | "
              f"total x{s['total_mult']:.2f} | maxDD {s['dd']:.1f}% | "
              f"win {s['win']:.0f}% | losing months {s['neg_months']}")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
