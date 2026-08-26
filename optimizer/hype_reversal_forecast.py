#!/usr/bin/env python3
"""Can HYPE reversals be forecast well enough to gate the take-profit?
(research only — follows hype_lev_tp_reentry_sim.py, where every
foresight-free TP/re-entry rule lost to holding.)

Model: at minute t, a first-passage race — will price move ADVERSELY by
d_thr (0.68% ~ 5 margin pts at 7.4x) before moving favorably by u_thr
(0.41% ~ 3 pts), within 24h? Two logistic models (numpy, no sklearn):
  P_dn  down-first  (adverse for LONGS)
  P_up  up-first    (adverse for SHORTS)
Features are causal 1m OHLCV transforms (returns, EMA gaps, realized vol,
RSI, range, distance to recent high/low, volume/trade-count z, time of day).

Honesty protocol:
  * time split — train < 2026-03-01, test >= (no shuffling, no leakage)
  * skill on the test set reported as AUC + decile lift
  * VALUE test: the TP fires only when P_adverse >= tau, re-enter on 0.5%
    dip — replayed ONLY on the component's test-period trades vs plain hold
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "runs", "All_pairs_LEV_1m-3m_multi-strat")
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
D_THR, U_THR, HORIZON = 0.0068, 0.0041, 1440
SPLIT = np.datetime64("2026-03-01")


@njit(cache=False)
def first_passage(close, high, low, d_arr, u_arr, horizon, down_first):
    """Barriers are PER-MINUTE arrays — pass constants broadcast for the
    static version, or vol-scaled arrays (k x sigma60) for the adaptive one
    so the race means the same thing in calm and wild regimes."""
    n = len(close)
    y = np.full(n, -1.0)
    for i in range(n - 1):
        d_thr = d_arr[i]
        u_thr = u_arr[i]
        up_b = close[i] * (1.0 + u_thr)
        dn_b = close[i] * (1.0 - d_thr)
        if down_first == 0:            # race: up u_thr vs down d_thr
            up_b = close[i] * (1.0 + d_thr)
            dn_b = close[i] * (1.0 - u_thr)
        jmax = min(n, i + horizon)
        for j in range(i + 1, jmax):
            hit_up = high[j] >= up_b
            hit_dn = low[j] <= dn_b
            if hit_up and hit_dn:      # same bar: ambiguous — call it adverse
                y[i] = 1.0
                break
            if down_first == 1:
                if hit_dn:
                    y[i] = 1.0
                    break
                if hit_up:
                    y[i] = 0.0
                    break
            else:
                if hit_up:
                    y[i] = 1.0
                    break
                if hit_dn:
                    y[i] = 0.0
                    break
    return y


def ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def roll_std(x, w):
    c = np.cumsum(np.concatenate(([0.0], x)))
    c2 = np.cumsum(np.concatenate(([0.0], x * x)))
    n = len(x)
    out = np.full(n, np.nan)
    m = (c[w:] - c[:-w]) / w
    v = (c2[w:] - c2[:-w]) / w - m * m
    out[w - 1:] = np.sqrt(np.maximum(v, 0.0))
    return out


def roll_minmax(x, w, want_max):
    from collections import deque
    n = len(x)
    out = np.full(n, np.nan)
    dq = deque()
    for i in range(n):
        while dq and dq[0] <= i - w:
            dq.popleft()
        while dq and ((x[dq[-1]] <= x[i]) if want_max else (x[dq[-1]] >= x[i])):
            dq.pop()
        dq.append(i)
        if i >= w - 1:
            out[i] = x[dq[0]]
    return out


def build_features(df):
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    lo = df["low"].values.astype(np.float64)
    vol = df["volume"].values.astype(np.float64)
    tc = df["trades_count"].values.astype(np.float64)
    lc = np.log(c)
    r1 = np.diff(lc, prepend=lc[0])
    F = {}
    for k in (1, 5, 15, 60, 240, 720):
        F[f"r{k}"] = lc - np.roll(lc, k)
    for k in (10, 30, 120):
        F[f"ema{k}"] = c / ema(c, k) - 1.0
    F["vol60"] = roll_std(r1, 60)
    F["vol240"] = roll_std(r1, 240)
    d = np.zeros(len(c))
    u = np.maximum(r1, 0)
    dn = np.maximum(-r1, 0)
    au, ad = ema(u, 14), ema(dn, 14)
    F["rsi"] = 100 - 100 / (1 + au / np.maximum(ad, 1e-12)) - 50 + d
    hi240 = roll_minmax(h, 240, True)
    lo240 = roll_minmax(lo, 240, False)
    F["dhi"] = c / hi240 - 1.0
    F["dlo"] = c / lo240 - 1.0
    F["rng60"] = (roll_minmax(h, 60, True) - roll_minmax(lo, 60, False)) / c
    lv = np.log1p(vol)
    F["volz"] = (lv - pd.Series(lv).rolling(240).mean().values) / \
        np.maximum(pd.Series(lv).rolling(240).std().values, 1e-9)
    ltc = np.log1p(tc)
    F["tcz"] = (ltc - pd.Series(ltc).rolling(240).mean().values) / \
        np.maximum(pd.Series(ltc).rolling(240).std().values, 1e-9)
    hrs = df["t"].dt.hour.values + df["t"].dt.minute.values / 60.0
    F["hsin"] = np.sin(2 * np.pi * hrs / 24)
    F["hcos"] = np.cos(2 * np.pi * hrs / 24)
    X = np.column_stack(list(F.values()))
    return X, list(F.keys())


def fit_logreg(X, y, iters=400, lr=0.05, l2=1e-4):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xs = (X - mu) / sd
    w = np.zeros(X.shape[1])
    b = float(np.log(max(y.mean(), 1e-6) / max(1 - y.mean(), 1e-6)))
    mw = np.zeros_like(w)
    vw = np.zeros_like(w)
    mb = vb = 0.0
    for t in range(1, iters + 1):
        z = Xs @ w + b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        g = p - y
        gw = Xs.T @ g / len(y) + l2 * w
        gb = g.mean()
        mw = 0.9 * mw + 0.1 * gw
        vw = 0.999 * vw + 0.001 * gw * gw
        mb = 0.9 * mb + 0.1 * gb
        vb = 0.999 * vb + 0.001 * gb * gb
        w -= lr * (mw / 0.99) / (np.sqrt(vw / 0.7) + 1e-8)
        b -= lr * (mb / 0.99) / (np.sqrt(vb / 0.7) + 1e-8)
    return dict(w=w, b=b, mu=mu, sd=sd)


def predict(m, X):
    z = ((X - m["mu"]) / m["sd"]) @ m["w"] + m["b"]
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def auc(y, p):
    o = np.argsort(p)
    r = np.empty(len(p))
    r[o] = np.arange(1, len(p) + 1)
    n1 = y.sum()
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main():
    df = pd.read_parquet(os.path.join(DATA, "hype_1min.parquet"))
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(None)
    df = df.sort_values("t").reset_index(drop=True)
    t64 = df["t"].values.astype("datetime64[ns]")
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    lo = df["low"].values.astype(np.float64)
    print(f"{len(df)} minutes {df['t'].iloc[0]} .. {df['t'].iloc[-1]}")

    X, names = build_features(df)
    lc = np.log(c)
    r1 = np.diff(lc, prepend=lc[0])
    sig = roll_std(r1, 60)
    sig = np.where(np.isnan(sig) | (sig <= 0), np.nanmedian(sig), sig)
    if len(sys.argv) > 1 and sys.argv[1] == "--volscaled":
        # same MEDIAN barrier as the static run, but scaled per-minute with
        # realized vol so the race is regime-invariant
        kd = D_THR / np.median(sig)
        ku = U_THR / np.median(sig)
        d_arr = np.clip(kd * sig, 0.002, 0.03)
        u_arr = np.clip(ku * sig, 0.0012, 0.018)
        print(f"VOL-SCALED barriers: median dn "
              f"{100*np.median(d_arr):.2f}% / up {100*np.median(u_arr):.2f}%"
              f" (5th-95th pct dn {100*np.percentile(d_arr,5):.2f}"
              f"-{100*np.percentile(d_arr,95):.2f}%)")
    else:
        d_arr = np.full(len(c), D_THR)
        u_arr = np.full(len(c), U_THR)
    y_dn = first_passage(c, h, lo, d_arr, u_arr, HORIZON, 1)
    y_up = first_passage(c, h, lo, d_arr, u_arr, HORIZON, 0)
    ok = ~np.isnan(X).any(1)
    tr = ok & (t64 < SPLIT) & (np.arange(len(df)) % 3 == 0)
    te = ok & (t64 >= SPLIT)
    models = {}
    for tag, yy in (("down-first (adverse for longs)", y_dn),
                    ("up-first (adverse for shorts)", y_up)):
        m_tr = tr & (yy >= 0)
        m_te = te & (yy >= 0)
        mdl = fit_logreg(X[m_tr], yy[m_tr])
        p_te = predict(mdl, X[m_te])
        a = auc(yy[m_te], p_te)
        base = yy[m_te].mean()
        dec = np.percentile(p_te, [10 * i for i in range(1, 10)])
        hi = yy[m_te][p_te >= dec[-1]].mean()
        lop = yy[m_te][p_te <= dec[0]].mean()
        print(f"\n{tag}: train {int(m_tr.sum())} pts, test "
              f"{int(m_te.sum())} pts | base rate {base:.3f} | "
              f"test AUC {a:.3f} | top-decile rate {hi:.3f}, "
              f"bottom-decile {lop:.3f}")
        top = np.argsort(-np.abs(mdl["w"]))[:6]
        print("  strongest features: "
              + ", ".join(f"{names[i]} {mdl['w'][i]:+.2f}" for i in top))
        models[tag[:2]] = mdl

    # ---------- value test: gate the TP with the model, test trades only ----
    ts = t64.astype("int64")
    comps = []
    for f in sorted(glob.glob(os.path.join(RUN, "_t_hype_*.json"))):
        for run, tab in json.load(open(f)).items():
            trr = tab.get("trades") or []
            if not trr:
                continue
            et = np.array([x[0] for x in trr], dtype=np.int64)
            xt = np.array([x[1] for x in trr], dtype=np.int64)
            r = np.array([x[2] for x in trr])
            ei = np.clip(np.searchsorted(ts, et, side="right") - 1, 0, None)
            xi = np.clip(np.searchsorted(ts, xt, side="right") - 1, 0, None)
            epx, xpx = c[ei], c[xi]
            pm = xpx / np.maximum(epx, 1e-12) - 1.0
            dirs = np.where(r * pm >= 0, 1, -1)
            good = np.abs(pm) > 5e-4
            lev = (float(np.median(np.abs(r[good]) / np.abs(pm[good])))
                   if good.sum() >= 3 else 1.0)
            comps.append(dict(run=run, et=et, ei=ei, xi=xi, epx=epx,
                              xpx=xpx, dir=dirs,
                              lev=float(np.clip(round(lev, 1), 1, 100))))
    p_dn_all = predict(models["do"], X)
    p_up_all = predict(models["up"], X)
    FEE = 0.0004
    for comp in comps:
        sel = [k for k in range(len(comp["et"]))
               if comp["et"][k] >= SPLIT.astype("datetime64[ns]").astype("int64")]
        if not sel:
            continue
        lv = comp["lev"]
        base = 1.0
        for k in sel:
            d = int(comp["dir"][k])
            base *= max(1e-9, 1 + (comp["xpx"][k] / comp["epx"][k] - 1)
                        * d * lv - 2 * FEE * lv)
        print(f"\n{comp['run']} — {len(sel)} TEST trades (>= {SPLIT}), "
              f"lev~{lv}x | hold-to-exit: x{base:.2f}")
        for tau in (0.5, 0.55, 0.6, 0.65, 0.7):
            mult, fired = 1.0, 0
            for k in sel:
                d = int(comp["dir"][k])
                i0, i1 = int(comp["ei"][k]), int(comp["xi"][k])
                padv = p_dn_all if d > 0 else p_up_all
                in_pos, entry, m, exit_px = True, comp["epx"][k], 1.0, None
                for i in range(i0 + 1, i1 + 1):
                    if in_pos:
                        u = (c[i] / entry - 1) * d * lv
                        if u >= 0.03 and padv[i] >= tau:
                            m *= 1 + u - 2 * FEE * lv
                            in_pos, exit_px = False, c[i]
                            fired += 1
                    elif (exit_px - c[i]) * d >= 0.005 * exit_px:
                        in_pos, entry = True, c[i]
                if in_pos:
                    m *= 1 + (comp["xpx"][k] / entry - 1) * d * lv \
                        - 2 * FEE * lv
                mult *= max(m, 1e-9)
            tag = "BEATS HOLD" if mult > base else "worse"
            print(f"  tau {tau:.2f}: x{mult:8.2f} ({tag}) "
                  f"[{fired} gated TPs]")


if __name__ == "__main__":
    main()
