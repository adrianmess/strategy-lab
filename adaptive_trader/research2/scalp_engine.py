"""
Numba-jitted engine for the Scalp Strategy (VRVP + CVD + EMA).

TradingView semantics:
  - signals evaluated on bar close; entry fills next bar open
  - bracket exits (strategy.exit limit/stop from SIGNAL-bar close) fill
    INTRABAR using the broker-emulator path assumption:
      up bar  (close>=open): open -> low -> high -> close
      down bar (close<open): open -> high -> low -> close
  - gap-through fills at open
"""
import numpy as np
import pandas as pd
from numba import njit
from engine import rsi as rsi_tv, ema

SCALP_PARAM_NAMES = [
    "tpLong", "tpShort", "sl",             # 0-2 (fractions)
    "rsiOB", "rsiOS",                      # 3-4
    "leverage",                            # 5
    "enableLong", "enableShort",           # 6-7
    "useCvdBranch", "useEmaBranch",        # 8-9
    "slOn",                                # 10 (1=bracket stop active)
]
NSP = len(SCALP_PARAM_NAMES)

SCALP_DEFAULTS = dict(tpLong=0.006, tpShort=0.006, sl=0.05,
                      rsiOB=61.0, rsiOS=34.0, leverage=1.0,
                      enableLong=1.0, enableShort=1.0,
                      useCvdBranch=1.0, useEmaBranch=1.0, slOn=1.0)

def scalp_vec(overrides=None):
    d = dict(SCALP_DEFAULTS)
    if overrides:
        d.update(overrides)
    return np.array([d[k] for k in SCALP_PARAM_NAMES])

@njit(cache=True)
def _poc(volume, close, lookback):
    """close[i] of the max-volume bar in the last `lookback+1` bars (incl current)."""
    n = len(volume)
    out = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - lookback)
        mv = -1.0
        poc = np.nan
        for j in range(i, lo - 1, -1):  # Pine loop 0..lookback => current backwards
            if volume[j] > mv:
                mv = volume[j]
                poc = close[j]
        out[i] = poc
    return out

def scalp_precompute(df3: pd.DataFrame, p=None,
                     cvdLength=50, vrvpLength=300, rsiLength=14,
                     fastEmaLen=1, slowEmaLen=1200):
    o = df3["open"].to_numpy(); h = df3["high"].to_numpy()
    l = df3["low"].to_numpy(); c = df3["close"].to_numpy()
    v = df3["volume"].to_numpy().astype(np.float64)
    delta = np.where(c > o, v, np.where(c < o, -v, 0.0))
    cvd = np.cumsum(delta)
    cvdSMA = pd.Series(cvd).rolling(cvdLength).mean().to_numpy()
    r = rsi_tv(c, rsiLength)
    fe = c if fastEmaLen == 1 else ema(c, fastEmaLen)
    se = ema(c, slowEmaLen)
    n = len(c)
    cvdUp = np.zeros(n); cvdDn = np.zeros(n)
    cvdUp[1:] = ((cvd[1:] > cvdSMA[1:]) & (cvd[:-1] <= cvdSMA[:-1])).astype(float)
    cvdDn[1:] = ((cvd[1:] < cvdSMA[1:]) & (cvd[:-1] >= cvdSMA[:-1])).astype(float)
    emaBull = np.zeros(n); emaBear = np.zeros(n)
    emaBull[1:] = ((fe[1:] > se[1:]) & (fe[:-1] <= se[:-1])).astype(float)
    emaBear[1:] = ((fe[1:] < se[1:]) & (fe[:-1] >= se[:-1])).astype(float)
    poc = _poc(v, c, vrvpLength)
    t_ms = (df3["t"].astype("int64") // 10**6).to_numpy().astype(np.float64)
    return dict(t=df3["t"].to_numpy(), t_ms=t_ms, o=o, h=h, l=l, c=c, vol=v,
                rsi=r, cvdUp=cvdUp, cvdDn=cvdDn, aboveCvd=(cvd > cvdSMA).astype(float),
                belowCvd=(cvd < cvdSMA).astype(float),
                emaBull=emaBull, emaBear=emaBear, poc=poc)

MAXT = 30000

@njit(cache=True)
def _scalp_core(o, h, l, c, rsi, cvdUp, cvdDn, aboveCvd, belowCvd,
                emaBull, emaBear, poc, regime, P,
                warmup, initial_capital, commission,
                liq_threshold):
    """trade row: [entry_idx, exit_idx, dir, entry, exit, qty, net, mae,
                   reason(0=tp,1=sl,2=liq,3=eod), lev]"""
    n = len(c)
    equity = initial_capital
    pos = 0
    pend = 0  # pending entry dir
    pend_tp = 0.0; pend_sl = 0.0; pend_lev = 0.0
    qty = 0.0; entry_price = 0.0; tp_price = 0.0; sl_price = 0.0
    lev_used = 0.0; entry_idx = -1
    runMin = 1e18; runMax = -1e18
    trades = np.zeros((MAXT, 10))
    nt = 0
    liquidated = 0

    for i in range(n):
        # ---- entry fill at open ----
        if pend != 0 and pos == 0:
            pos = pend
            entry_price = o[i]
            tp_price = pend_tp; sl_price = pend_sl
            lev_used = pend_lev
            qty = equity / entry_price * lev_used  # qty uses signal-bar equity approx entry
            runMin = 1e18; runMax = -1e18
            entry_idx = i
            pend = 0

        # ---- intrabar exits (bracket) ----
        if pos != 0:
            if l[i] < runMin: runMin = l[i]
            if h[i] > runMax: runMax = h[i]
            exit_px = np.nan
            reason = -1
            up_bar = c[i] >= o[i]
            slOn = P[regime[i], 10] > 0
            if pos > 0:
                gap_sl = slOn and o[i] <= sl_price
                gap_tp = o[i] >= tp_price
                hit_sl = slOn and l[i] <= sl_price
                hit_tp = h[i] >= tp_price
                if gap_sl:
                    exit_px = o[i]; reason = 1
                elif gap_tp:
                    exit_px = o[i]; reason = 0
                elif up_bar:  # open->low->high
                    if hit_sl: exit_px = sl_price; reason = 1
                    elif hit_tp: exit_px = tp_price; reason = 0
                else:         # open->high->low
                    if hit_tp: exit_px = tp_price; reason = 0
                    elif hit_sl: exit_px = sl_price; reason = 1
                # liquidation check (no-SL mode)
                thr = liq_threshold if liq_threshold > 0 else (1.0 / lev_used - 0.008)
                if reason != 1 and l[i] / entry_price - 1.0 <= -thr:
                    exit_px = entry_price * (1 - thr); reason = 2
            else:
                gap_sl = slOn and o[i] >= sl_price
                gap_tp = o[i] <= tp_price
                hit_sl = slOn and h[i] >= sl_price
                hit_tp = l[i] <= tp_price
                if gap_sl:
                    exit_px = o[i]; reason = 1
                elif gap_tp:
                    exit_px = o[i]; reason = 0
                elif up_bar:  # open->low->high: TP (low side) first for short
                    if hit_tp: exit_px = tp_price; reason = 0
                    elif hit_sl: exit_px = sl_price; reason = 1
                else:
                    if hit_sl: exit_px = sl_price; reason = 1
                    elif hit_tp: exit_px = tp_price; reason = 0
                thr = liq_threshold if liq_threshold > 0 else (1.0 / lev_used - 0.008)
                if reason != 1 and -(h[i] / entry_price - 1.0) <= -thr:
                    exit_px = entry_price * (1 + thr); reason = 2

            if reason >= 0:
                gross = qty * (exit_px - entry_price) * pos
                fee = commission * qty * (entry_price + exit_px)
                net = gross - fee
                if reason == 2:
                    net = -equity
                equity += net
                if pos > 0:
                    adverse = min(runMin, exit_px) / entry_price - 1.0
                else:
                    adverse = -(max(runMax, exit_px) / entry_price - 1.0)
                if nt < MAXT:
                    trades[nt, 0] = entry_idx; trades[nt, 1] = i
                    trades[nt, 2] = pos; trades[nt, 3] = entry_price
                    trades[nt, 4] = exit_px; trades[nt, 5] = qty
                    trades[nt, 6] = net; trades[nt, 7] = adverse
                    trades[nt, 8] = reason; trades[nt, 9] = lev_used
                    nt += 1
                pos = 0
                if equity <= 0 or reason == 2:
                    liquidated = 1
                    break

        # ---- signal on bar close ----
        if i < warmup or pos != 0 or pend != 0:
            continue
        r = regime[i]
        longCond = P[r, 6] > 0 and rsi[i] < P[r, 4] and c[i] > poc[i] and (
            (P[r, 8] > 0 and aboveCvd[i] > 0 and cvdUp[i] > 0)
            or (P[r, 9] > 0 and emaBull[i] > 0))
        shortCond = P[r, 7] > 0 and rsi[i] > P[r, 3] and c[i] < poc[i] and (
            (P[r, 8] > 0 and belowCvd[i] > 0 and cvdDn[i] > 0)
            or (P[r, 9] > 0 and emaBear[i] > 0))
        if longCond:
            pend = 1
            pend_tp = c[i] * (1 + P[r, 0])
            pend_sl = c[i] * (1 - P[r, 2])
            pend_lev = P[r, 5]
        elif shortCond:
            pend = -1
            pend_tp = c[i] * (1 - P[r, 1])
            pend_sl = c[i] * (1 + P[r, 2])
            pend_lev = P[r, 5]

    return trades[:nt], equity, liquidated


def run_scalp(pre, P, regime=None, warmup=1300, initial_capital=100.0,
              commission=0.0004, liq_threshold=1e9):
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    if P.ndim == 1:
        P = P.reshape(1, -1)
    tr, eq, liq = _scalp_core(pre["o"], pre["h"], pre["l"], pre["c"], pre["rsi"],
                              pre["cvdUp"], pre["cvdDn"], pre["aboveCvd"], pre["belowCvd"],
                              pre["emaBull"], pre["emaBear"], pre["poc"],
                              regime.astype(np.int32), P.astype(np.float64),
                              warmup, initial_capital, commission, liq_threshold)
    cols = ["entry_idx", "exit_idx", "dir", "entry", "exit", "qty", "net",
            "mae", "reason", "lev"]
    df = pd.DataFrame(tr, columns=cols)
    if len(df):
        df["entry_t"] = pre["t"][df["entry_idx"].astype(int)]
        df["exit_t"] = pre["t"][df["exit_idx"].astype(int)]
    return df, eq, bool(liq)
