"""
Numba-jitted backtest core for parameter sweeps.
Indicator series are precomputed once (lengths fixed); the jitted loop takes
threshold/exit/cooldown/leverage params. Supports:
  - normal mode (SL on)
  - live mode (SL off + liquidation at maintenance threshold)
  - per-bar leverage array (for adaptive leverage)
  - per-bar parameter regime index with per-regime param matrix
"""
import numpy as np
import pandas as pd
from numba import njit
from engine import (DEFAULT_PARAMS, ema, rma, rsi, atr, sma, stdev_pop,
                    macd_line, last_1m_metric)

# ---- parameter vector layout (per regime) ----
PARAM_NAMES = [
    "rsiValLong", "macdValPctLong", "bbValLong",          # 0-2
    "ptLong", "apt1Long", "apt2Long", "dur1Long", "dur2Long",  # 3-7
    "cdPctLong", "cdPeriodLong",                          # 8-9
    "rsiValShort", "macdValPctShort", "bbValShort",       # 10-12
    "ptShort", "apt1Short", "apt2Short", "dur1Short", "dur2Short",  # 13-17
    "cdPctShort", "cdPeriodShort",                        # 18-19
    "xTpLong", "xApt1Long", "xApt2Long", "xDur1Long", "xDur2Long",  # 20-24
    "xTpShort", "xApt1Short", "xApt2Short", "xDur1Short", "xDur2Short",  # 25-29
    "xMacdMinShort", "xMacdMaxLong", "xMinBetween",       # 30-32
    "xCdPctShort", "xCdPeriodShort",                      # 33-34
    "slLong", "slShort", "leverage",                      # 35-37
    "enableLong3m", "enableShort3m", "enableLongX", "enableShortX",  # 38-41
    "xSlLong", "xSlShort",                                # 42-43 (cross-system stops)
    "xCdPctLong", "xCdPeriodLong",                        # 44-45 (cross-long cooldown)
    "requireHistPos",                                     # 46
]
NP = len(PARAM_NAMES)

def params_to_vec(p, overrides=None):
    d = dict(
        rsiValLong=p["rsiValLong"], macdValPctLong=p["macdValPctLong"], bbValLong=p["bbValLong"],
        ptLong=p["ptLong"], apt1Long=p["apt1Long"], apt2Long=p["apt2Long"],
        dur1Long=p["dur1Long"], dur2Long=p["dur2Long"],
        cdPctLong=p["cdPctLong"], cdPeriodLong=p["cdPeriodLong"],
        rsiValShort=p["rsiValShort"], macdValPctShort=p["macdValPctShort"], bbValShort=p["bbValShort"],
        ptShort=p["ptShort"], apt1Short=p["apt1Short"], apt2Short=p["apt2Short"],
        dur1Short=p["dur1Short"], dur2Short=p["dur2Short"],
        cdPctShort=p["cdPctShort"], cdPeriodShort=p["cdPeriodShort"],
        xTpLong=p["xTpLong"], xApt1Long=p["xApt1Long"], xApt2Long=p["xApt2Long"],
        xDur1Long=p["xDur1Long"], xDur2Long=p["xDur2Long"],
        xTpShort=p["xTpShort"], xApt1Short=p["xApt1Short"], xApt2Short=p["xApt2Short"],
        xDur1Short=p["xDur1Short"], xDur2Short=p["xDur2Short"],
        xMacdMinShort=p["xMacdMinShort"], xMacdMaxLong=p["xMacdMaxLong"], xMinBetween=p["xMinBetween"],
        xCdPctShort=p["xCdPctShort"], xCdPeriodShort=p["xCdPeriodShort"],
        slLong=p["slLong"], slShort=p["slShort"], leverage=p["leverage"],
        enableLong3m=1.0, enableShort3m=1.0, enableLongX=1.0, enableShortX=1.0,
        xSlLong=p["xSlLong"], xSlShort=p["xSlShort"],
        xCdPctLong=p.get("xCdPctLong", 99.0), xCdPeriodLong=p.get("xCdPeriodLong", 90),
        requireHistPos=1.0 if p.get("xRequireHistPos", True) else 0.0,
    )
    if overrides:
        d.update(overrides)
    return np.array([d[k] for k in PARAM_NAMES], dtype=np.float64)

def precompute(df3: pd.DataFrame, df1: pd.DataFrame, p=DEFAULT_PARAMS):
    """Compute all indicator arrays once (uses default lengths)."""
    o = df3["open"].to_numpy(); h = df3["high"].to_numpy()
    l = df3["low"].to_numpy(); c = df3["close"].to_numpy()
    a = atr(h, l, c, p["atrLength_3m"])
    emaLL, emaLH = ema(c, p["emaLongLenLow"]), ema(c, p["emaLongLenHigh"])
    emaSL, emaSH = ema(c, p["emaShortLenLow"]), ema(c, p["emaShortLenHigh"])
    emaLong = np.where(a > p["volThreshLong"], emaLH, emaLL)
    emaShort = np.where(a > p["volThreshShort"], emaSH, emaSL)
    emaLongUp = (np.diff(emaLong, prepend=np.nan) > 0).astype(np.float64)
    emaShortDown = (np.diff(emaShort, prepend=np.nan) < 0).astype(np.float64)
    rsiL = rsi(c, p["rsiLenLong"])
    macdL = macd_line(c, p["macdFastLong"], p["macdSlowLong"])
    basisL = sma(c, p["bbLenLong"]); devL = p["bbStdLong"] * stdev_pop(c, p["bbLenLong"])
    bbPctL = (c - (basisL - devL)) / (2 * devL)
    xMacd = macd_line(c, p["xFast"], p["xSlow"])
    xSignal = ema(xMacd, p["xSig"])
    xHist = xMacd - xSignal
    n = len(c)
    xUp = np.zeros(n); xDn = np.zeros(n)
    xUp[1:] = ((xMacd[1:] > xSignal[1:]) & (xMacd[:-1] <= xSignal[:-1])).astype(float)
    xDn[1:] = ((xMacd[1:] < xSignal[1:]) & (xMacd[:-1] >= xSignal[:-1])).astype(float)
    nrb = p["xHistRisingBars"]
    histRising = np.ones(n, bool)
    for k in range(nrb):
        cur = np.roll(xHist, k); cur[:k] = np.nan
        nxt = np.roll(xHist, k + 1); nxt[:k + 1] = np.nan
        histRising &= (cur > nxt)
    cprev = np.roll(c, 1); cprev[0] = np.nan
    m1 = last_1m_metric(df3, df1)
    m3 = cprev - c
    cdMetricL = (m1 if p["cdTfLong"] == "1" else m3) / cprev     # priceDropLong
    cdMetricS = -(m1 if p["cdTfShort"] == "1" else m3) / cprev   # priceIncreaseShort
    cdMetricXS = -(m1 if p["xCdTfShort"] == "1" else m3) / cprev
    cdMetricXL = (m1 if p.get("xCdTfLong", "1") == "1" else m3) / cprev
    t_ms = (df3["t"].astype("int64") // 10**6).to_numpy()
    return dict(t=df3["t"].to_numpy(), t_ms=t_ms.astype(np.float64),
                o=o, h=h, l=l, c=c, vol=df3["volume"].to_numpy(),
                rsiL=rsiL, macdL=macdL, bbPctL=bbPctL,
                emaLongUp=emaLongUp, emaShortDown=emaShortDown,
                xMacd=xMacd, xHist=xHist, xUp=xUp, xDn=xDn,
                histRising=histRising.astype(np.float64),
                cdMetricL=cdMetricL, cdMetricS=cdMetricS, cdMetricXS=cdMetricXS,
                cdMetricXL=cdMetricXL)

MAX_TRADES = 20000

@njit(cache=True)
def _core(t_ms, o, h, l, c,
          rsiL, macdL, bbPctL, emaLongUp, emaShortDown,
          xMacd, xHist, xUp, xDn, histRising,
          cdMetricL, cdMetricS, cdMetricXS, cdMetricXL,
          regime,            # int32 per bar
          P,                 # (n_regimes, NP) param matrix
          warmup, initial_capital, commission,
          use_sl, liq_threshold):
    """Returns trade array + final equity + liquidation flag.
    trade row: [entry_idx, exit_idx, dir, system(0=3m,1=cross), entry, exit,
                qty, net, mae, reason(0=pt,1=sl,2=liq), lev]
    """
    n = len(c)
    equity = initial_capital
    pos = 0
    pend_entry = 0   # 0 none, else dir
    pend_sys = 0
    pend_qty = 0.0; pend_sl = 0.0; pend_et = 0.0; pend_lev = 0.0
    pend_exit = -1
    qty = 0.0; entry_price = 0.0; entry_time = 0.0; slPrice = 0.0
    sys_ = 0; lev_used = 0.0
    runMin = 1e18; runMax = -1e18
    entry_idx = -1
    cdStartL = -1e18; cdStartS = -1e18; cdStartXS = -1e18; cdStartXL = -1e18
    lastLongBarX = -10**9; lastShortBarX = -10**9
    trades = np.zeros((MAX_TRADES, 11))
    nt = 0
    liquidated = 0

    for i in range(n):
        r = regime[i]
        # fills
        if pend_exit >= 0 and pos != 0:
            px = o[i]
            gross = qty * (px - entry_price) * pos
            fee = commission * qty * (entry_price + px)
            net = gross - fee
            equity += net
            if pos > 0:
                adverse = runMin / entry_price - 1.0
            else:
                adverse = -(runMax / entry_price - 1.0)
            if nt < MAX_TRADES:
                trades[nt, 0] = entry_idx; trades[nt, 1] = i
                trades[nt, 2] = pos; trades[nt, 3] = sys_
                trades[nt, 4] = entry_price; trades[nt, 5] = px
                trades[nt, 6] = qty; trades[nt, 7] = net
                trades[nt, 8] = adverse; trades[nt, 9] = pend_exit
                trades[nt, 10] = lev_used
                nt += 1
            pos = 0; pend_exit = -1; qty = 0.0
            if equity <= 0:
                liquidated = 1
                break
        if pend_entry != 0 and pos == 0:
            pos = pend_entry; qty = pend_qty; entry_price = o[i]
            slPrice = pend_sl; sys_ = pend_sys; entry_time = pend_et
            lev_used = pend_lev
            runMin = 1e18; runMax = -1e18
            entry_idx = i
            pend_entry = 0
        if pos != 0:
            if l[i] < runMin: runMin = l[i]
            if h[i] > runMax: runMax = h[i]
            # liquidation check in live mode (intrabar adverse move)
            if use_sl == 0:
                if pos > 0:
                    adverse_now = l[i] / entry_price - 1.0
                else:
                    adverse_now = -(h[i] / entry_price - 1.0)
                thr = liq_threshold
                if thr < 0:  # dynamic: per-trade leverage determines liq distance
                    thr = 1.0 / lev_used - 0.008
                if adverse_now <= -thr:
                    # margin wiped: lose equity committed (whole account here)
                    eq_before = equity
                    equity = 0.0
                    if nt < MAX_TRADES:
                        trades[nt, 0] = entry_idx; trades[nt, 1] = i
                        trades[nt, 2] = pos; trades[nt, 3] = sys_
                        trades[nt, 4] = entry_price
                        trades[nt, 5] = entry_price * (1 - thr * pos)
                        trades[nt, 6] = qty; trades[nt, 7] = -eq_before
                        trades[nt, 8] = -thr; trades[nt, 9] = 2
                        trades[nt, 10] = lev_used
                        nt += 1
                    liquidated = 1
                    break

        if i < warmup:
            continue
        tm = t_ms[i]

        # cooldown updates (regime-dependent thresholds)
        if cdMetricL[i] <= -P[r, 8]: cdStartL = tm
        if cdMetricS[i] >= P[r, 18]: cdStartS = tm
        if cdMetricXS[i] >= P[r, 33]: cdStartXS = tm
        if cdMetricXL[i] <= -P[r, 44]: cdStartXL = tm
        actL = (tm - cdStartL) < P[r, 9] * 60000
        actS = (tm - cdStartS) < P[r, 19] * 60000
        actXS = (tm - cdStartXS) < P[r, 34] * 60000
        actXL = (tm - cdStartXL) < P[r, 45] * 60000

        long3m = (P[r, 38] > 0 and rsiL[i] < P[r, 0] and macdL[i] < P[r, 1] * c[i]
                  and bbPctL[i] < P[r, 2] and emaLongUp[i] > 0 and not actL)
        short3m = (P[r, 39] > 0 and rsiL[i] > P[r, 10] and macdL[i] > P[r, 11] * c[i]
                   and bbPctL[i] > P[r, 12] and emaShortDown[i] > 0 and not actS)
        gapBars = P[r, 32] * 20.0
        canL = (i - lastLongBarX) > gapBars
        canS = (i - lastShortBarX) > gapBars
        longX = (P[r, 40] > 0 and xUp[i] > 0 and xMacd[i] < P[r, 31]
                 and histRising[i] > 0 and (P[r, 46] <= 0 or xHist[i] > 0)
                 and not actXL and canL)
        shortX = (P[r, 41] > 0 and xDn[i] > 0 and xMacd[i] > P[r, 30]
                  and not actXS and canS)

        if pos == 0:
            lev = P[r, 37]
            if long3m or longX:
                q = equity / c[i] * lev
                if long3m:
                    pend_entry = 1; pend_sys = 0; pend_qty = q
                    pend_sl = c[i] * (1 - P[r, 35]); pend_et = tm; pend_lev = lev
                else:
                    pend_entry = 1; pend_sys = 1; pend_qty = q
                    pend_sl = c[i] * (1 - P[r, 42]); pend_et = tm; pend_lev = lev
                    lastLongBarX = i
            if short3m or shortX:
                q = equity / c[i] * lev
                if short3m:
                    pend_entry = -1; pend_sys = 0; pend_qty = q
                    pend_sl = c[i] * (1 + P[r, 36]); pend_et = tm; pend_lev = lev
                else:
                    pend_entry = -1; pend_sys = 1; pend_qty = q
                    pend_sl = c[i] * (1 + P[r, 43]); pend_et = tm; pend_lev = lev
                    lastShortBarX = i

        if pos > 0:
            if use_sl == 1 and l[i] <= slPrice:
                pend_exit = 1
            else:
                if sys_ == 0:
                    pt = P[r, 3]
                    if tm >= entry_time + P[r, 7] * 60000: pt = P[r, 5]
                    elif tm >= entry_time + P[r, 6] * 60000: pt = P[r, 4]
                else:
                    pt = P[r, 20]
                    if tm >= entry_time + P[r, 24] * 60000: pt = P[r, 22]
                    elif tm >= entry_time + P[r, 23] * 60000: pt = P[r, 21]
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
                    pt = P[r, 25]
                    if tm >= entry_time + P[r, 29] * 60000: pt = P[r, 27]
                    elif tm >= entry_time + P[r, 28] * 60000: pt = P[r, 26]
                if c[i] <= entry_price * (1 - pt):
                    pend_exit = 0

    open_pos = np.zeros(6)
    if pos != 0:
        open_pos[0] = pos; open_pos[1] = entry_idx; open_pos[2] = entry_price
        open_pos[3] = qty; open_pos[4] = lev_used; open_pos[5] = 1.0
    return trades[:nt], equity, liquidated, open_pos


def run_fast(pre, P, regime=None, warmup=3000, initial_capital=1000.0,
             commission=0.0004, use_sl=True, liq_threshold=0.12, return_open=False):
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    if P.ndim == 1:
        P = P.reshape(1, -1)
    cdXL = pre.get("cdMetricXL", pre["cdMetricL"])
    tr, eq, liq, _op = _core(pre["t_ms"], pre["o"], pre["h"], pre["l"], pre["c"],
                        pre["rsiL"], pre["macdL"], pre["bbPctL"],
                        pre["emaLongUp"], pre["emaShortDown"],
                        pre["xMacd"], pre["xHist"], pre["xUp"], pre["xDn"],
                        pre["histRising"],
                        pre["cdMetricL"], pre["cdMetricS"], pre["cdMetricXS"], cdXL,
                        regime.astype(np.int32), P.astype(np.float64),
                        warmup, initial_capital, commission,
                        1 if use_sl else 0, liq_threshold)
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
