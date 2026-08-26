#!/usr/bin/env python3
"""Wide combinatorial search for an intra-trade overlay on the HYPE LEV
component (research only). Follows the narrow-grid + forecast studies, which
both favored plain hold-to-mirror-exit.

Policy space (~114k policies), each replayed minute-by-minute through every
historical virtual trade of the component:
  TP        none, +2, +3, +5, +8, +12, +20, +30, +50  (margin pts)
  TP gate   none, 20 indicator conditions, and all pairs (condition AND
            condition) — the TP only fires while the gate holds
  SL        none, -5, -10, -15, -20  (margin pts; ungated)
  re-entry  never | price dips {0.25,0.5,1,2,3,5,10}% below our exit
            (dir-aware) | virtual-basis unrealized <= {0,-3,-5,-8} pts
  after re-entry the policy keeps running (multi-cycle); still-open at the
  component's exit bar -> mirror exit
Fees: engine-parity 0.04%/side x lev per leg.

Honesty protocol:
  * trades split by ENTRY time: train < 2026-03-01 (report ranks on train
    ONLY), test >= 2026-03-01 (reported for the ranked survivors)
  * the headline number is the train->test generalization picture, not the
    best cell
Run:  python3 hype_overlay_search.py [workers]         (HYPE only)
      python3 hype_overlay_search.py [workers] --all   (ALL pairs pooled:
          919 trades across the router's 13 components — 12x the sample,
          the honest way to ask whether ANY overlay family generalizes)
"""
import glob
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "runs", "All_pairs_LEV_1m-3m_multi-strat")
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
FEE_SIDE = 0.0004
SPLIT_DATE = os.environ.get("SPLIT_DATE", "2026-03-01")
SPLIT_NS = int(np.datetime64(SPLIT_DATE).astype("datetime64[ns]")
               .astype("int64"))

TPS = [0.0, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20, 0.30, 0.50]   # 0 = none
SLS = [0.0, 0.05, 0.10, 0.15, 0.20]                            # 0 = none
RE_KINDS = [(0, 0.0)] + [(1, d) for d in
                         (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)] \
    + [(2, v) for v in (0.0, -0.03, -0.05, -0.08)]

_G = {}


def _ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _roll_std(x, w):
    c = np.cumsum(np.concatenate(([0.0], x)))
    c2 = np.cumsum(np.concatenate(([0.0], x * x)))
    out = np.full(len(x), np.nan)
    m = (c[w:] - c[:-w]) / w
    v = (c2[w:] - c2[:-w]) / w - m * m
    out[w - 1:] = np.sqrt(np.maximum(v, 0.0))
    return out


def _roll_extreme(x, w, want_max):
    from collections import deque
    out = np.full(len(x), np.nan)
    dq = deque()
    for i in range(len(x)):
        while dq and dq[0] <= i - w:
            dq.popleft()
        while dq and ((x[dq[-1]] <= x[i]) if want_max
                      else (x[dq[-1]] >= x[i])):
            dq.pop()
        dq.append(i)
        if i >= w - 1:
            out[i] = x[dq[0]]
    return out


ALL_PAIRS = "--all" in sys.argv


def _pair_block(pair):
    """(ts, close, gate-matrix, gnames) for one pair's 1m series."""
    df = pd.read_parquet(os.path.join(DATA, f"{pair}_1min.parquet"))
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(None)
    df = df.sort_values("t").reset_index(drop=True)
    ts = df["t"].values.astype("datetime64[ns]").astype("int64")
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    lo = df["low"].values.astype(np.float64)
    vol = df["volume"].values.astype(np.float64)
    lc = np.log(c)
    r1 = np.diff(lc, prepend=lc[0])
    e10, e30 = _ema(c, 10), _ema(c, 30)
    e12, e26 = _ema(c, 12), _ema(c, 26)
    macd = e12 - e26
    hist = macd - _ema(macd, 9)
    au = _ema(np.maximum(r1, 0), 14)
    ad = _ema(np.maximum(-r1, 0), 14)
    rsi = 100 - 100 / (1 + au / np.maximum(ad, 1e-12))
    sma20 = pd.Series(c).rolling(20).mean().values
    sd20 = pd.Series(c).rolling(20).std().values
    bb = (c - (sma20 - 2 * sd20)) / np.maximum(4 * sd20, 1e-9)
    lv = np.log1p(vol)
    volz = (lv - pd.Series(lv).rolling(240).mean().values) / \
        np.maximum(pd.Series(lv).rolling(240).std().values, 1e-9)
    sig60, sig240 = _roll_std(r1, 60), _roll_std(r1, 240)
    hi240 = _roll_extreme(h, 240, True)
    dhi = c / np.maximum(hi240, 1e-9) - 1.0
    r15 = lc - np.roll(lc, 15)
    r60 = lc - np.roll(lc, 60)
    conds = [
        ("rsi>70", rsi > 70), ("rsi<30", rsi < 30),
        ("rsi>60", rsi > 60), ("rsi<40", rsi < 40),
        ("c<ema10", c < e10), ("c>ema10", c > e10),
        ("ema10<ema30", e10 < e30), ("ema10>ema30", e10 > e30),
        ("macdh<0", hist < 0), ("macdh_fall", hist < np.roll(hist, 3)),
        ("%B>1", bb > 1), ("%B<0", bb < 0),
        ("%B>.8", bb > 0.8), ("%B<.2", bb < 0.2),
        ("volz>2", volz > 2), ("vol_expand", sig60 > 1.3 * sig240),
        ("r15<0", r15 < 0), ("r60<0", r60 < 0),
        ("at_4h_high", dhi > -0.001), ("off_4h_high>1%", dhi < -0.01),
    ]
    gnames = [n for n, _ in conds]
    G = np.column_stack([np.where(np.isnan(v.astype(np.float64)), 0,
                                  v).astype(np.uint8)
                         if v.dtype != np.bool_ else v.astype(np.uint8)
                         for _, v in conds])
    return ts, c, G, gnames


def build_world():
    """Concatenate per-pair blocks; trade indices are offset into the big
    arrays, so the kernel is pair-agnostic."""
    pairs = (["btc", "doge", "eth", "hype", "sol", "sui", "xrp"]
             if ALL_PAIRS else ["hype"])
    blocks = {}
    cs, Gs = [], []
    gnames, off = None, 0
    for p in pairs:
        ts, c, G, gnames = _pair_block(p)
        blocks[p] = (ts, off, c)
        cs.append(c)
        Gs.append(G)
        off += len(c)
    c_all = np.concatenate(cs)
    G_all = np.vstack(Gs)

    pat = "_t_*.json" if ALL_PAIRS else "_t_hype_*.json"
    trades = []
    for f in sorted(glob.glob(os.path.join(RUN, pat))):
        pair = os.path.basename(f).split("_")[2]
        if pair not in blocks:
            continue
        ts, off, c = blocks[pair]
        for run, tab in json.load(open(f)).items():
            tr = tab.get("trades") or []
            if not tr:
                continue
            et = np.array([x[0] for x in tr], dtype=np.int64)
            xt = np.array([x[1] for x in tr], dtype=np.int64)
            r = np.array([x[2] for x in tr])
            ei = np.clip(np.searchsorted(ts, et, side="right") - 1, 0, None)
            xi = np.clip(np.searchsorted(ts, xt, side="right") - 1, 0, None)
            epx, xpx = c[ei], c[xi]
            pm = xpx / np.maximum(epx, 1e-12) - 1.0
            dirs = np.where(r * pm >= 0, 1, -1)
            good = np.abs(pm) > 5e-4
            lev = (float(np.median(np.abs(r[good]) / np.abs(pm[good])))
                   if good.sum() >= 3 else 1.0)
            lev = float(np.clip(round(lev, 1), 1.0, 100.0))
            for k in range(len(tr)):
                trades.append((int(et[k]), int(off + ei[k]),
                               int(off + xi[k]),
                               float(epx[k]), float(xpx[k]),
                               int(dirs[k]), lev))
    trades.sort()
    tr_arr = np.array([(t[1], t[2], t[3], t[4], t[5], t[6],
                        1 if t[0] < SPLIT_NS else 0)
                       for t in trades], dtype=np.float64)
    return c_all, G_all, gnames, tr_arr


def _init():
    from numba import njit
    c, G, gnames, tr_arr = build_world()
    _G["c"] = c
    _G["G"] = G
    _G["names"] = gnames
    _G["tr"] = tr_arr

    @njit(cache=False)
    def eval_policy(c, G, tr, tp, g1, g2, sl, rk, rp):
        m_train, m_test = 1.0, 1.0
        cycles = 0
        for t in range(tr.shape[0]):
            i0 = int(tr[t, 0])
            i1 = int(tr[t, 1])
            epx = tr[t, 2]
            xpx = tr[t, 3]
            d = tr[t, 4]
            lv = tr[t, 5]
            is_train = tr[t, 6] > 0.5
            fee = 2.0 * FEE_SIDE * lv
            m = 1.0
            in_pos = True
            entry = epx
            exit_px = 0.0
            for i in range(i0 + 1, i1 + 1):
                p = c[i]
                if in_pos:
                    u = (p / entry - 1.0) * d * lv
                    fire = False
                    if tp > 0.0 and u >= tp:
                        ok1 = True if g1 < 0 else G[i, g1] == 1
                        ok2 = True if g2 < 0 else G[i, g2] == 1
                        fire = ok1 and ok2
                    if not fire and sl > 0.0 and u <= -sl:
                        fire = True
                    if fire:
                        m *= 1.0 + u - fee
                        in_pos = False
                        exit_px = p
                        cycles += 1
                elif rk == 1:
                    if (exit_px - p) * d >= rp / 100.0 * exit_px:
                        in_pos = True
                        entry = p
                elif rk == 2:
                    if (p / epx - 1.0) * d * lv <= rp * 100.0 * 0.01:
                        in_pos = True
                        entry = p
            if in_pos:
                m *= 1.0 + (xpx / entry - 1.0) * d * lv - fee
            if m < 1e-9:
                m = 1e-9
            if is_train:
                m_train *= m
            else:
                m_test *= m
        return m_train, m_test, cycles

    _G["eval"] = eval_policy
    # warm the jit
    _G["eval"](_G["c"], _G["G"], _G["tr"], 0.05, -1, -1, 0.0, 1, 1.0)


def _work(chunk):
    ev = _G["eval"]
    out = []
    for (tp, g1, g2, sl, rk, rp) in chunk:
        mt, me, cy = ev(_G["c"], _G["G"], _G["tr"],
                        tp, g1, g2, sl, rk, rp)
        out.append((mt, me, cy, tp, g1, g2, sl, rk, rp))
    return out


def main():
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    nproc = int(nums[0]) if nums else 12
    c, G, gnames, tr_arr = build_world()
    n_tr = int(tr_arr[:, 6].sum())
    n_te = tr_arr.shape[0] - n_tr
    print(f"{tr_arr.shape[0]} trades ({n_tr} train / {n_te} test), "
          f"{len(gnames)} gate conditions", flush=True)

    gate_ids = [(-1, -1)] + [(i, -1) for i in range(len(gnames))] \
        + [(i, j) for i in range(len(gnames))
           for j in range(i + 1, len(gnames))]
    pols = []
    for tp in TPS:
        gates = gate_ids if tp > 0 else [(-1, -1)]
        for (g1, g2) in gates:
            for sl in SLS:
                if tp == 0.0 and sl == 0.0:
                    continue
                for (rk, rp) in RE_KINDS:
                    pols.append((tp, g1, g2, sl, rk, rp))
    print(f"{len(pols)} policies; {nproc} workers", flush=True)

    chunks = [pols[i::nproc * 8] for i in range(nproc * 8)]
    t0 = time.time()
    with Pool(nproc, initializer=_init) as pool:
        res = []
        for k, part in enumerate(pool.imap_unordered(_work, chunks)):
            res.extend(part)
            print(f"  chunk {k+1}/{len(chunks)} "
                  f"({time.time()-t0:.0f}s, {len(res)} done)", flush=True)

    # hold baseline
    def hold():
        m_tr = m_te = 1.0
        for t in range(tr_arr.shape[0]):
            d, lv = tr_arr[t, 4], tr_arr[t, 5]
            m = 1 + (tr_arr[t, 3] / tr_arr[t, 2] - 1) * d * lv \
                - 2 * FEE_SIDE * lv
            if tr_arr[t, 6] > 0.5:
                m_tr *= max(m, 1e-9)
            else:
                m_te *= max(m, 1e-9)
        return m_tr, m_te
    b_tr, b_te = hold()
    print(f"\nHOLD baseline: train x{b_tr:.2f} | test x{b_te:.2f}")

    res.sort(reverse=True)                    # by train multiple
    arr = np.array([(r[0], r[1]) for r in res])
    print(f"policies beating hold on TRAIN: "
          f"{int((arr[:,0] > b_tr).sum())}/{len(res)} "
          f"({100*(arr[:,0] > b_tr).mean():.1f}%)")
    print(f"policies beating hold on TEST : "
          f"{int((arr[:,1] > b_te).sum())}/{len(res)} "
          f"({100*(arr[:,1] > b_te).mean():.1f}%)")
    top = res[:max(1, len(res)//100)]
    ttop = np.array([r[1] for r in top])
    print(f"top-1% by TRAIN: {int((ttop > b_te).sum())}/{len(top)} beat "
          f"hold on TEST (test multiples: median x{np.median(ttop):.2f}, "
          f"best x{ttop.max():.2f})")

    def gname(g):
        return "-" if g < 0 else gnames[g]

    def rname(rk, rp):
        return ("never" if rk == 0 else
                f"dip {rp}%" if rk == 1 else f"virt<={100*rp:.0f}")
    print("\nTOP 25 BY TRAIN (with their TEST result):")
    for mt, me, cy, tp, g1, g2, sl, rk, rp in res[:25]:
        print(f"  train x{mt:10.2f} | test x{me:8.2f} "
              f"({'BEATS' if me > b_te else 'worse':>5} hold) | "
              f"TP {100*tp:g} gate[{gname(g1)}"
              + (f" & {gname(g2)}" if g2 >= 0 else "")
              + f"] SL {100*sl:g} re[{rname(rk, rp)}] cyc {cy}")
    # ---- family stability check: the recurring winner shape from the
    # 2026-03 split — TP +8, no SL, re-enter dip 1% or at virtual entry ----
    fam = [r for r in res
           if r[3] == 0.08 and r[6] == 0.0
           and ((r[7] == 1 and r[8] == 1.0) or (r[7] == 2 and r[8] == 0.0))]
    if fam:
        f_te = np.array([r[1] for r in fam])
        print(f"\nFAMILY (TP+8, re dip1%%|virt<=0, any gate): {len(fam)} "
              f"cells | beat hold on TEST: {int((f_te > b_te).sum())} "
              f"({100*(f_te > b_te).mean():.0f}%) | median test "
              f"x{np.median(f_te):.2f} vs hold x{b_te:.2f}")

    print("\nTOP 10 BY TEST (hindsight — for context only):")
    for mt, me, cy, tp, g1, g2, sl, rk, rp in \
            sorted(res, key=lambda r: -r[1])[:10]:
        print(f"  test x{me:10.2f} | train x{mt:8.2f} | "
              f"TP {100*tp:g} gate[{gname(g1)}"
              + (f" & {gname(g2)}" if g2 >= 0 else "")
              + f"] SL {100*sl:g} re[{rname(rk, rp)}]")
    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
