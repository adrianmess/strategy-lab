"""
Engine v3 — EVERY parameter searchable, including indicator lengths.

Indicator lengths can't be recomputed per candidate (too slow), so we
precompute a LIBRARY of variants once (stacked 2-D arrays: variants x bars)
and each candidate/regime selects variants by INDEX. The jitted core then
reads e.g. rsi_all[P[r, C_VRSI], i]. Evaluation cost stays ~milliseconds.

Variant menus (edit VARIANTS below, then delete engine3 caches to rebuild):
  rsi     : RSI length
  macd    : entry-MACD (fast, slow) — z-scored vs its own trailing 3d vol
  bb      : Bollinger (length, stddev) -> %B
  ema     : trend EMA length -> up/down flags
  xmacd   : crossover-MACD (fast, slow, signal) — z-scored line
  histn   : "histogram rising for N bars" requirement

Everything else (thresholds, PTs, durations, cooldowns, leverage, stops,
enables) is a continuous/direct column in the P matrix, per regime.
"""
import os, pickle
import numpy as np
import pandas as pd
from numba import njit

from engine import ema, rsi as rsi_tv, sma, stdev_pop, DEFAULT_PARAMS
from fast_engine import last_1m_metric
from regimes import regime_features, DAY

_DEFAULT_VARIANTS = dict(
    rsi=[2, 3, 4, 6, 9, 14],
    macd=[(3, 7), (3, 10), (5, 13), (8, 17), (12, 26)],
    bb=[(14, 2.0), (21, 2.0), (30, 2.0), (21, 2.5), (50, 2.0)],
    ema=[20, 50, 95, 150, 270],
    xmacd=[(12, 26, 9), (12, 26, 8), (8, 17, 9), (5, 35, 5), (20, 50, 9)],
    histn=[2, 4, 8, 12],
)

def _load_variants():
    """User-editable indicator-length libraries (optimizer/param_space.json,
    v7.variants). Falls back to the built-in defaults."""
    import json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "optimizer", "param_space.json")
    try:
        v = json.load(open(path)).get("v7", {}).get("variants")
        if v:
            return dict(
                rsi=[int(x) for x in v["rsi"]],
                macd=[tuple(map(float, x)) for x in v["macd"]],
                bb=[tuple(map(float, x)) for x in v["bb"]],
                ema=[int(x) for x in v["ema"]],
                xmacd=[tuple(map(float, x)) for x in v["xmacd"]],
                histn=[int(x) for x in v["histn"]],
            )
    except Exception as e:
        print(f"variants from param_space.json unusable ({e}); using defaults")
    return dict(_DEFAULT_VARIANTS)

VARIANTS = _load_variants()

def variants_hash():
    import hashlib
    return hashlib.md5(repr(sorted(VARIANTS.items())).encode()).hexdigest()[:8]
ZWIN = 3 * DAY  # z-score window for MACD lines

# ---- P-matrix layout (per regime) ----
P3_NAMES = [
    # entry thresholds (z units for macd; raw for rsi/bb)
    "rsiValLong", "rsiValShort", "zL", "zS", "bbValLong", "bbValShort",  # 0-5
    "zXS", "zXLmax",                                                     # 6-7
    # exits: base PT, adjusted PT1/PT2, durations (min) — per side, 3m system
    "ptLong", "apt1Long", "apt2Long", "dur1Long", "dur2Long",            # 8-12
    "ptShort", "apt1Short", "apt2Short", "dur1Short", "dur2Short",       # 13-17
    # cross-system exits
    "xTpLong", "xApt1Long", "xApt2Long", "xDur1Long", "xDur2Long",       # 18-22
    "xTpShort", "xApt1Short", "xApt2Short", "xDur1Short", "xDur2Short",  # 23-27
    # cooldowns
    "cdPctLong", "cdPeriodLong", "cdPctShort", "cdPeriodShort",          # 28-31
    "xCdPctLong", "xCdPeriodLong", "xCdPctShort", "xCdPeriodShort",      # 32-35
    "xMinBetween",                                                       # 36
    # risk
    "slLong", "slShort", "leverage",                                     # 37-39
    # enables
    "eL3", "eS3", "eXL", "eXS", "requireHistPos",                        # 40-44
    # variant indices
    "vRsi", "vMacd", "vBB", "vEmaUp", "vEmaDn", "vX", "vHistN",          # 45-51
    # trend block for shorts: z threshold (99 = off)
    "trendBlockZ",                                                       # 52
]
C = {k: i for i, k in enumerate(P3_NAMES)}
NP3 = len(P3_NAMES)

DEFAULTS3 = dict(
    rsiValLong=68.0, rsiValShort=68.0, zL=-1.7, zS=1.7, bbValLong=-0.05, bbValShort=0.825,
    zXS=2.0, zXLmax=-99.0,
    ptLong=0.012, apt1Long=0.007, apt2Long=0.00407, dur1Long=0.6, dur2Long=14.4,
    ptShort=0.010, apt1Short=0.009, apt2Short=0.004, dur1Short=0.6, dur2Short=14.4,
    xTpLong=0.004, xApt1Long=0.003, xApt2Long=0.00207, xDur1Long=60.0, xDur2Long=120.0,
    xTpShort=0.0015, xApt1Short=0.001, xApt2Short=0.0, xDur1Short=60.0, xDur2Short=120.0,
    cdPctLong=0.00402, cdPeriodLong=90.0, cdPctShort=0.003, cdPeriodShort=70.0,
    xCdPctLong=0.99, xCdPeriodLong=90.0, xCdPctShort=0.0003, xCdPeriodShort=7.0,
    xMinBetween=13.0,
    slLong=0.10, slShort=0.10, leverage=8.0,
    eL3=1.0, eS3=1.0, eXL=0.0, eXS=1.0, requireHistPos=1.0,
    vRsi=1, vMacd=0, vBB=1, vEmaUp=2, vEmaDn=4, vX=1, vHistN=3,
    trendBlockZ=99.0,
)

def vec3(overrides=None):
    d = dict(DEFAULTS3)
    if overrides:
        d.update(overrides)
    return np.array([d[k] for k in P3_NAMES], dtype=np.float64)

# ---------------- variant library precompute ----------------

def roll_std(x, win):
    return pd.Series(x).rolling(win, min_periods=win // 4).std().to_numpy()

def zscore(x):
    s = roll_std(x, ZWIN)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(s > 0, x / s, np.nan)

def precompute3(df3: pd.DataFrame, df1: pd.DataFrame):
    o = df3["open"].to_numpy(); h = df3["high"].to_numpy()
    l = df3["low"].to_numpy(); c = df3["close"].to_numpy()
    n = len(c)
    rsi_all = np.vstack([rsi_tv(c, L) for L in VARIANTS["rsi"]])
    macdz_all = np.vstack([zscore(ema(c, int(f)) - ema(c, int(s))) for f, s in VARIANTS["macd"]])
    bb_all = []
    for L, sd in [(int(a), float(b)) for a, b in VARIANTS["bb"]]:
        basis = sma(c, L); dev = sd * stdev_pop(c, L)
        bb_all.append((c - (basis - dev)) / (2 * dev))
    bb_all = np.vstack(bb_all)
    emaup_all, emadn_all = [], []
    for L in VARIANTS["ema"]:
        e = ema(c, L)
        d = np.diff(e, prepend=np.nan)
        emaup_all.append((d > 0).astype(np.float64))
        emadn_all.append((d < 0).astype(np.float64))
    emaup_all = np.vstack(emaup_all); emadn_all = np.vstack(emadn_all)
    xz_all, xup_all, xdn_all, xhist_all = [], [], [], []
    hist_rising = []
    for f, s, sg in [(int(a), int(b), int(g)) for a, b, g in VARIANTS["xmacd"]]:
        line = ema(c, f) - ema(c, s)
        sig = ema(line, sg)
        hist = line - sig
        xz = zscore(line)
        up = np.zeros(n); dn = np.zeros(n)
        up[1:] = ((line[1:] > sig[1:]) & (line[:-1] <= sig[:-1])).astype(float)
        dn[1:] = ((line[1:] < sig[1:]) & (line[:-1] >= sig[:-1])).astype(float)
        xz_all.append(xz); xup_all.append(up); xdn_all.append(dn); xhist_all.append(hist)
        for nrb in VARIANTS["histn"]:
            hr = np.ones(n, bool)
            for k in range(nrb):
                cur = np.roll(hist, k); cur[:k] = np.nan
                nxt = np.roll(hist, k + 1); nxt[:k + 1] = np.nan
                hr &= (cur > nxt)
            hist_rising.append(hr.astype(np.float64))
    xz_all = np.vstack(xz_all); xup_all = np.vstack(xup_all)
    xdn_all = np.vstack(xdn_all); xhist_all = np.vstack(xhist_all)
    hist_all = np.vstack(hist_rising)  # row = vX * len(histn) + vHistN

    cprev = np.roll(c, 1); cprev[0] = np.nan
    m1 = last_1m_metric(df3, df1)
    m3 = cprev - c
    cd1 = m1 / cprev      # 1-minute "drop" metric (sign per original semantics)
    cd3 = m3 / cprev

    f = regime_features(dict(c=c, h=h, l=l, vol=df3["volume"].to_numpy()))
    t_ms = (df3["t"].astype("int64") // 10**6).to_numpy().astype(np.float64)
    return dict(t=df3["t"].to_numpy(), t_ms=t_ms, o=o, h=h, l=l, c=c,
                vol=df3["volume"].to_numpy(),
                rsi_all=rsi_all, macdz_all=macdz_all, bb_all=bb_all,
                emaup_all=emaup_all, emadn_all=emadn_all,
                xz_all=xz_all, xup_all=xup_all, xdn_all=xdn_all,
                xhist_all=xhist_all, hist_all=hist_all,
                cd1=cd1, cd3=cd3, trend=f["trend"], feats=f)

# ---------------- jitted core ----------------
MAXT3 = 30000
NHIST = len(VARIANTS["histn"])

@njit(cache=True)
def _core3(t_ms, o, h, l, c,
           rsi_all, macdz_all, bb_all, emaup_all, emadn_all,
           xz_all, xup_all, xdn_all, xhist_all, hist_all,
           cd1, cd3, trend,
           regime, P, warmup, initial_capital, commission,
           use_sl, dyn_liq, no_entry):
    n = len(c)
    equity = initial_capital
    pos = 0; pend = 0; pend_sys = 0
    pend_qty = 0.0; pend_sl = 0.0; pend_et = 0.0; pend_lev = 0.0
    pend_exit = -1
    qty = 0.0; entry_price = 0.0; entry_time = 0.0; slPrice = 0.0
    sys_ = 0; lev_used = 0.0
    runMin = 1e18; runMax = -1e18; entry_idx = -1
    cdL = -1e18; cdS = -1e18; cdXL = -1e18; cdXS = -1e18
    lastXL = -10**9; lastXS = -10**9
    trades = np.zeros((MAXT3, 11))
    nt = 0
    liquidated = 0

    for i in range(n):
        if pend_exit >= 0 and pos != 0:
            px = o[i]
            gross = qty * (px - entry_price) * pos
            fee = commission * qty * (entry_price + px)
            net = gross - fee
            equity += net
            adverse = (runMin / entry_price - 1.0) if pos > 0 else -(runMax / entry_price - 1.0)
            if nt < MAXT3:
                trades[nt, 0] = entry_idx; trades[nt, 1] = i
                trades[nt, 2] = pos; trades[nt, 3] = sys_
                trades[nt, 4] = entry_price; trades[nt, 5] = px
                trades[nt, 6] = qty; trades[nt, 7] = net
                trades[nt, 8] = adverse; trades[nt, 9] = pend_exit
                trades[nt, 10] = lev_used
                nt += 1
            pos = 0; pend_exit = -1; qty = 0.0
            if equity <= 0:
                liquidated = 1; break
        if pend != 0 and pos == 0:
            pos = pend; qty = pend_qty; entry_price = o[i]
            slPrice = pend_sl; sys_ = pend_sys; entry_time = pend_et
            lev_used = pend_lev
            runMin = 1e18; runMax = -1e18; entry_idx = i
            pend = 0
        if pos != 0:
            if l[i] < runMin: runMin = l[i]
            if h[i] > runMax: runMax = h[i]
            if use_sl == 0 and dyn_liq == 1:
                adv_now = (l[i] / entry_price - 1.0) if pos > 0 else -(h[i] / entry_price - 1.0)
                thr = 1.0 / lev_used - 0.008
                if adv_now <= -thr:
                    eqb = equity; equity = 0.0
                    if nt < MAXT3:
                        trades[nt, 0] = entry_idx; trades[nt, 1] = i
                        trades[nt, 2] = pos; trades[nt, 3] = sys_
                        trades[nt, 4] = entry_price
                        trades[nt, 5] = entry_price * (1 - thr * pos)
                        trades[nt, 6] = qty; trades[nt, 7] = -eqb
                        trades[nt, 8] = -thr; trades[nt, 9] = 2
                        trades[nt, 10] = lev_used
                        nt += 1
                    liquidated = 1; break

        if i < warmup or no_entry[i] == 1:
            continue
        r = regime[i]
        tm = t_ms[i]
        # cooldowns (long metrics use the 1-minute series per original)
        if cd1[i] <= -P[r, 28]: cdL = tm
        if -cd3[i] >= P[r, 30]: cdS = tm
        if cd1[i] <= -P[r, 32]: cdXL = tm
        if -cd3[i] >= P[r, 34]: cdXS = tm
        actL = (tm - cdL) < P[r, 29] * 60000
        actS = (tm - cdS) < P[r, 31] * 60000
        actXL = (tm - cdXL) < P[r, 33] * 60000
        actXS = (tm - cdXS) < P[r, 35] * 60000

        vr = int(P[r, 45]); vm = int(P[r, 46]); vb = int(P[r, 47])
        vu = int(P[r, 48]); vd = int(P[r, 49]); vx = int(P[r, 50]); vh = int(P[r, 51])
        rsiv = rsi_all[vr, i]
        mz = macdz_all[vm, i]
        bbv = bb_all[vb, i]
        xz = xz_all[vx, i]
        if not (np.isfinite(mz) and np.isfinite(xz)):
            continue
        blockShort = trend[i] > P[r, 52]

        long3m = (P[r, 40] > 0 and rsiv < P[r, 0] and mz < P[r, 2]
                  and bbv < P[r, 4] and emaup_all[vu, i] > 0 and not actL)
        short3m = (P[r, 41] > 0 and rsiv > P[r, 1] and mz > P[r, 3]
                   and bbv > P[r, 5] and emadn_all[vd, i] > 0 and not actS
                   and not blockShort)
        gapBars = P[r, 36] * 20.0
        canL = (i - lastXL) > gapBars
        canS = (i - lastXS) > gapBars
        hist_row = vx * NHIST + vh
        longX = (P[r, 42] > 0 and xup_all[vx, i] > 0 and xz < P[r, 7]
                 and hist_all[hist_row, i] > 0
                 and (P[r, 44] <= 0 or xhist_all[vx, i] > 0)
                 and not actXL and canL)
        shortX = (P[r, 43] > 0 and xdn_all[vx, i] > 0 and xz > P[r, 6]
                  and not actXS and canS and not blockShort)

        if pos == 0:
            lev = P[r, 39]
            if long3m or longX:
                q = equity / c[i] * lev
                pend = 1; pend_qty = q; pend_et = tm; pend_lev = lev
                if long3m:
                    pend_sys = 0; pend_sl = c[i] * (1 - P[r, 37])
                else:
                    pend_sys = 1; pend_sl = c[i] * (1 - P[r, 37])
                    lastXL = i
            if short3m or shortX:
                q = equity / c[i] * lev
                pend = -1; pend_qty = q; pend_et = tm; pend_lev = lev
                if short3m:
                    pend_sys = 0; pend_sl = c[i] * (1 + P[r, 38])
                else:
                    pend_sys = 1; pend_sl = c[i] * (1 + P[r, 38])
                    lastXS = i

        if pos > 0:
            if use_sl == 1 and l[i] <= slPrice:
                pend_exit = 1
            else:
                if sys_ == 0:
                    pt = P[r, 8]
                    if tm >= entry_time + P[r, 12] * 60000: pt = P[r, 10]
                    elif tm >= entry_time + P[r, 11] * 60000: pt = P[r, 9]
                else:
                    pt = P[r, 18]
                    if tm >= entry_time + P[r, 22] * 60000: pt = P[r, 20]
                    elif tm >= entry_time + P[r, 21] * 60000: pt = P[r, 19]
                if c[i] >= entry_price * (1 + pt):
                    pend_exit = 0
        elif pos < 0:
            if use_sl == 1 and h[i] >= slPrice:
                pend_exit = 1
            else:
                if sys_ == 0:
                    pt = P[r, 13]
                    if tm >= entry_time + P[r, 17] * 60000: pt = P[r, 15]
                    elif tm >= entry_time + P[r, 16] * 60000: pt = P[r, 14]
                else:
                    pt = P[r, 23]
                    if tm >= entry_time + P[r, 27] * 60000: pt = P[r, 25]
                    elif tm >= entry_time + P[r, 26] * 60000: pt = P[r, 24]
                if c[i] <= entry_price * (1 - pt):
                    pend_exit = 0

    open_pos = np.zeros(6)
    if pos != 0:
        open_pos[0] = pos; open_pos[1] = entry_idx; open_pos[2] = entry_price
        open_pos[3] = qty; open_pos[4] = lev_used; open_pos[5] = 1.0
    return trades[:nt], equity, liquidated, open_pos


def run3(pre, P, regime=None, warmup=3000, initial_capital=1000.0,
         commission=0.0004, use_sl=True, dyn_liq=True, return_open=False,
         no_entry=None):
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    if P.ndim == 1:
        P = P.reshape(1, -1)
    if no_entry is None:
        no_entry = np.zeros(n, dtype=np.int8)
    tr, eq, liq, _op = _core3(pre["t_ms"], pre["o"], pre["h"], pre["l"], pre["c"],
                         pre["rsi_all"], pre["macdz_all"], pre["bb_all"],
                         pre["emaup_all"], pre["emadn_all"],
                         pre["xz_all"], pre["xup_all"], pre["xdn_all"],
                         pre["xhist_all"], pre["hist_all"],
                         pre["cd1"], pre["cd3"], pre["trend"],
                         regime.astype(np.int32), P.astype(np.float64),
                         warmup, initial_capital, commission,
                         1 if use_sl else 0, 1 if dyn_liq else 0, no_entry)
    cols = ["entry_idx", "exit_idx", "dir", "system", "entry", "exit",
            "qty", "net", "mae", "reason", "lev"]
    df = pd.DataFrame(tr, columns=cols)
    if len(df):
        df["entry_t"] = pre["t"][df["entry_idx"].astype(int)]
        df["exit_t"] = pre["t"][df["exit_idx"].astype(int)]
    if return_open:
        op = None
        if _op[0] != 0:
            op = dict(dir=int(_op[0]), entry_idx=int(_op[1]), entry=float(_op[2]),
                      qty=float(_op[3]), lev=float(_op[4]),
                      entry_t=str(pre["t"][int(_op[1])]))
        return df, eq, bool(liq), op
    return df, eq, bool(liq)


def get_pres3(cache="engine3_pre.pkl"):
    if os.path.exists(cache):
        try:
            return pickle.load(open(cache, "rb"))
        except Exception as e:
            print(f"cache {cache} unreadable ({e}); rebuilding...")
            try: os.remove(cache)
            except OSError: pass
    from common import load_segments
    pres = [precompute3(g, d1) for g, d1 in load_segments()]
    pickle.dump(pres, open(cache, "wb"))
    return pres
