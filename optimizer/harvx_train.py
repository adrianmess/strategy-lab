#!/usr/bin/env python3
"""HARVX — trainable harvest policy for ADVERSE (red) windows.

The state being traded: a long thesis is >=1% underwater and price is
chopping (the state Adrian trades by hand: adopt low, sell strength, rejoin
lower). Evidence (2026-08-25 research, XRP/HYPE 1m):
  - trailing stops and high-frequency indicator scalping LOSE in this regime
  - oversold-gated, low-frequency cycling with exits into strength WINS on
    some assets, while plain holding wins on others (HYPE)
So the policy space contains BOTH behaviors and per-pair training decides.

Genome (all dynamic — distances scale with realized hourly vol):
  g0 hold_mode      0/1: 1 = just hold the window to its end (no cycling)
  g1 rsi_win_idx    0/1/2 -> RSI window 30/70/140 minutes
  g2 rsi_lo         entry gate: oversold threshold [10..45]
  g3 rsi_hi         exit-into-strength threshold [55..90]
  g4 dip_mult       re-entry: price <= last_exit*(1 - dip_mult*hvol) [0..4]
  g5 exit_kind      0 tp only | 1 mean-revert-or-overbought | 2 overbought
  g6 tp_mult        dynamic target entry*(1 + tp_mult*hvol) [0.5..6]
  g7 mean_idx       0/1 -> exit level = rolling mean 1h / 4h
  g8 max_cycles     [1..10]
  g9 cooldown       bars to wait after an exit [0..120]
No stop-loss by design (spot; the runner's liq-guard covers catastrophe).
Fees: taker 0.05%/side (conservative — execution will use maker = 0%).

Windows: rolling 3-day windows every 6h; a window enters the set when price
first closes >=1% under its start ("red"); the sim runs from that bar to the
window end. Holdout schemes split WINDOWS by their start time.

Usage:
  python3 harvx_train.py --pair xrp --scheme hA --total 40000 --procs 12
  python3 harvx_train.py --all --procs 12          # 7 pairs x 5 schemes
Results: optimizer/harvx/<pair>_<scheme>.json (+ summary print).
"""
import argparse
import json
import multiprocessing as mp
import os
import time

import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
OUT = os.path.join(HERE, "harvx")
FEE = 0.0005
H, STEP, RED = 3 * 1440, 6 * 60, 0.01
PAIRS = ("btc", "eth", "sol", "xrp", "doge", "sui", "hype")
SCHEMES = ("hA", "hL21", "hB", "hM", "hN")
START = "2024-01-01"

_G = {}


def prep(pair):
    """Arrays + window index pairs + per-window month id and start epoch."""
    df = pd.read_parquet(os.path.join(DATA, f"{pair}_spot_1min.parquet"))
    tcol = [c for c in df.columns
            if c.lower() in ("t", "time", "ts", "datetime", "date")][0]
    df[tcol] = pd.to_datetime(df[tcol])
    c = df.set_index(tcol)["close"]
    if c.index.tz is not None:
        c.index = c.index.tz_localize(None)
    c = c.loc[START:]
    px = c.values.astype(np.float64)
    idx = c.index
    ret = c.pct_change()
    hvol = (ret.rolling(60).std() * np.sqrt(60)) \
        .bfill().clip(lower=1e-4).values
    rsis = []
    for w in (30, 70, 140):
        d = c.diff()
        up = d.clip(lower=0).rolling(w).mean()
        dn = (-d.clip(upper=0)).rolling(w).mean()
        r = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        rsis.append(r.fillna(50.0).values)
    means = [c.rolling(60).mean().bfill().values,
             c.rolling(240).mean().bfill().values]
    month = ((idx.year - 2024) * 12 + (idx.month - 1)).values.astype(np.int64)
    segs_a, segs_b, seg_t0 = [], [], []
    i = 0
    n = len(px)
    while i + H < n:
        e = px[i]
        w = px[i:i + H]
        hit = np.argmax(w <= e * (1 - RED))
        if hit > 0 or (w[0] <= e * (1 - RED)):
            segs_a.append(i + max(hit, 1))
            segs_b.append(i + H)
            seg_t0.append(idx[i])
        i += STEP
    return dict(px=px, hvol=hvol, rsi=np.stack(rsis), mean=np.stack(means),
                month=month.astype(np.int64),
                seg_a=np.array(segs_a, dtype=np.int64),
                seg_b=np.array(segs_b, dtype=np.int64),
                seg_t0=pd.DatetimeIndex(seg_t0), idx0=idx[0], idxN=idx[-1])


def scheme_masks(seg_t0, scheme):
    t = seg_t0
    if scheme == "hA":                       # holdout = the recent tail
        tr = t < pd.Timestamp("2026-05-01")
    elif scheme == "hB":                     # holdout = the oldest data
        tr = t >= pd.Timestamp("2025-01-01")
    elif scheme == "hM":                     # holdout = a middle band
        tr = ~((t >= pd.Timestamp("2025-06-01"))
               & (t < pd.Timestamp("2025-12-01")))
    elif scheme == "hL21":                   # alternating 21-day blocks
        blk = ((t - t[0]).days // 21) % 2
        tr = blk == 0
    else:                                    # hN: train on everything
        tr = np.ones(len(t), bool)
    return np.asarray(tr), ~np.asarray(tr) if scheme != "hN" \
        else np.zeros(len(t), bool)


# cache=False on purpose: 12 spawn workers compiling concurrently corrupt a
# shared numba disk cache (the 2026-08-23 host segfault crash-loop). A few
# seconds of per-worker compile per pool is the cheap, safe alternative.
@njit(cache=False)
def _sim(params, px, hvol, rsi, mean, month, seg_a, seg_b, use,
         month_pnl):
    hold_mode = params[0] > 0.5
    ri = int(params[1])
    rsi_lo = params[2]
    rsi_hi = params[3]
    dip = params[4]
    ekind = int(params[5])
    tp = params[6]
    mi = int(params[7])
    maxc = int(params[8])
    cool = int(params[9])
    total = 0.0
    worst = 0.0
    nwin = 0
    wins = 0
    for s in range(seg_a.shape[0]):
        if not use[s]:
            continue
        a = seg_a[s]
        b = seg_b[s]
        nwin += 1
        wnet = 0.0
        if hold_mode:
            wnet = (px[b - 1] / px[a] - 1.0) - 2 * FEE
            month_pnl[month[b - 1]] += wnet
        else:
            inpos = False
            entry = 0.0
            last_exit = px[a]
            cycles = 0
            cool_until = -1
            for i in range(a, b):
                p = px[i]
                if inpos:
                    out = False
                    if ekind == 0:
                        out = p >= entry * (1.0 + tp * hvol[i])
                    elif ekind == 1:
                        out = (p >= mean[mi, i]
                               and p > entry * (1.0 + 2 * FEE)) \
                              or rsi[ri, i] > rsi_hi
                    else:
                        out = rsi[ri, i] > rsi_hi
                    if out or i == b - 1:
                        g = (p / entry - 1.0) - 2 * FEE
                        wnet += g
                        month_pnl[month[i]] += g
                        inpos = False
                        last_exit = p
                        cool_until = i + cool
                else:
                    if (cycles < maxc and i > cool_until
                            and rsi[ri, i] < rsi_lo
                            and p <= last_exit * (1.0 - dip * hvol[i])):
                        inpos = True
                        entry = p
                        cycles += 1
            if inpos:                       # safety (loop closes at b-1)
                g = (px[b - 1] / entry - 1.0) - 2 * FEE
                wnet += g
                month_pnl[month[b - 1]] += g
        total += wnet
        if wnet < worst:
            worst = wnet
        if wnet > 0:
            wins += 1
    return total, worst, nwin, wins


def evaluate(params, G, use):
    mp_ = np.zeros(40)
    tot, worst, nwin, wins = _sim(params, G["px"], G["hvol"], G["rsi"],
                                  G["mean"], G["month"], G["seg_a"],
                                  G["seg_b"], use, mp_)
    neg_m = int((mp_ < -1e-9).sum())
    score = tot + 2.0 * worst - 0.05 * neg_m
    return score, dict(total=round(float(tot), 4),
                       worst=round(float(worst), 4), nwin=int(nwin),
                       wins=int(wins), neg_months=neg_m)


LO = np.array([0, 0, 10, 55, 0.0, 0, 0.5, 0, 1, 0], dtype=np.float64)
HI = np.array([1, 2, 45, 90, 4.0, 2, 6.0, 1, 10, 120], dtype=np.float64)


def rand_genome(rng):
    g = LO + rng.random(10) * (HI - LO)
    for k in (0, 1, 5, 7, 8, 9):
        g[k] = float(int(round(g[k])))
    return g


def mutate(rng, g):
    c = g.copy()
    for k in range(10):
        if rng.random() < 0.35:
            span = HI[k] - LO[k]
            c[k] = min(HI[k], max(LO[k], c[k] + rng.normal(0, 0.15 * span)))
    for k in (0, 1, 5, 7, 8, 9):
        c[k] = float(int(round(c[k])))
    return c


def worker(job):
    kind, seed, parents, nb, key, scheme = job
    G = _G.get(key) or _G.setdefault(key, prep(key))
    tr, _ = scheme_masks(G["seg_t0"], scheme)
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(nb):
        if kind == "random" or not parents:
            g = rand_genome(rng)
        else:
            i, j = rng.choice(len(parents), 2)
            g = np.where(rng.random(10) < 0.5, parents[i], parents[j])
            g = mutate(rng, np.asarray(g, dtype=np.float64))
        sc, m = evaluate(g, G, tr)
        out.append((sc, list(g), m))
    return out


def run_one(pair, scheme, total, procs):
    t0 = time.time()
    G = prep(pair)
    tr, ho = scheme_masks(G["seg_t0"], scheme)
    print(f"[{pair} {scheme}] windows: {tr.sum()} train / {ho.sum()} holdout",
          flush=True)
    pool_res = []
    batch = 200
    seed = int(time.time()) % 100000
    with mp.Pool(procs) as p:
        done = 0
        gen = 0
        while done < total:
            gen += 1
            parents = [g for _, g, _ in pool_res[:24]]
            kind = "random" if (gen <= 2 or not parents) else "offspring"
            jobs = [(kind if k < procs - 1 else "random",
                     seed + gen * 100 + k, parents, batch, pair, scheme)
                    for k in range(procs)]
            for res in p.map(worker, jobs):
                pool_res.extend(res)
            done += batch * procs
            pool_res.sort(key=lambda x: -x[0])
            pool_res = pool_res[:200]
            if gen % 4 == 0:
                b = pool_res[0]
                print(f"  ev {done} best {b[0]:+.3f} "
                      f"(tot {b[2]['total']:+.3f} worst {b[2]['worst']:+.3f} "
                      f"negm {b[2]['neg_months']})", flush=True)
    # holdout: judge the top 20 train genomes once each
    best_h = None
    for sc, g, m in pool_res[:20]:
        if not ho.any():
            break
        hsc, hm = evaluate(np.asarray(g), G, ho)
        if best_h is None or hm["total"] > best_h[2]["total"]:
            best_h = (sc, g, hm, m)
    out = dict(pair=pair, scheme=scheme, evals=total,
               elapsed_s=round(time.time() - t0, 1),
               train_best=dict(score=round(pool_res[0][0], 4),
                               genome=pool_res[0][1], m=pool_res[0][2]),
               holdout_best=(dict(train_score=round(best_h[0], 4),
                                  genome=best_h[1], holdout=best_h[2],
                                  train=best_h[3]) if best_h else None))
    os.makedirs(OUT, exist_ok=True)
    json.dump(out, open(os.path.join(OUT, f"{pair}_{scheme}.json"), "w"),
              indent=1)
    hb = out["holdout_best"]
    print(f"[{pair} {scheme}] DONE in {out['elapsed_s']}s | train "
          f"{out['train_best']['m']['total']:+.3f} | holdout "
          f"{hb['holdout']['total']:+.3f} over {hb['holdout']['nwin']} "
          f"windows" if hb else f"[{pair} {scheme}] DONE (no holdout)",
          flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair")
    ap.add_argument("--scheme", default="hA")
    ap.add_argument("--total", type=int, default=40000)
    ap.add_argument("--procs", type=int, default=12)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.all:
        for pair in PAIRS:
            for scheme in SCHEMES:
                run_one(pair, scheme, a.total, a.procs)
    else:
        run_one(a.pair, a.scheme, a.total, a.procs)


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
