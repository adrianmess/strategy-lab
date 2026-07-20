"""
Python replication of the Pine strategy
"V4 ROC[-1] 45 Closed - Leveraged ROC (Lowest Price + time Frame) and SMA
 Trend Strategy" (MEXC SOLUSDT.P, 3-min chart). Long-only.

Logic (pine-exact):
- roc = security("1", ta.roc(low, rocLength)): ROC of 1-minute LOWS, sampled at
  each 3-min chart bar close (last completed 1-min bar).
- "ROC trend": the sampled series must satisfy roc[k+1] > roc[k] for
  k = 1..rocNumBars-1 (i.e. STRICTLY FALLING toward the present over the last
  rocNumBars chart-bar samples — the [-1] dip signature in the title).
- smaValue = security("45", ta.sma(close, smaLength)): SMA of 45-minute closes
  (last COMPLETED 45-min bucket), sampled per chart bar; upward slope =
  smaValue > smaValue[smaNumBars] on chart-bar samples.
- entry only on the FIRST chart bar of a new 45-min bucket, all conditions met;
  strategy.entry with qty = leverage * equity / close (pyramiding=1: re-signals
  while long are REJECTED but still RESET longEntryPrice and the trailing stop
  to the current close — faithful side effects, they loosen the stop).
- "trailing stop" (pine-exact, NOT a from-the-high ratchet): while close > trail,
  trail := close*(1-trailPct) — it follows the close DOWN as well, so the exit
  fires only when a single close drops trailPct below the previous close.
- OPT-IN tslMode (P col "tslm", default 0 = pine above): 1 = RATCHET — the trail
  only ever moves UP (trail = max(trail, close*(1-trailPct))), so it both trails
  the high-water close and caps the loss from the signal close at ~trailPct.
  NOT pine-faithful; do not use for validation against TradingView exports.
- exits checked on bar close (TP: close >= longEntryPrice*(1+pt); trail:
  close <= trail), market close fills at NEXT bar open.
Validated against the 2026-07-12 TradingView export.
"""
import numpy as np
import pandas as pd
from numba import njit

# variant libraries (indicator lengths -> one precompute each, menu-searchable)
ROCX_ROC_LENGTHS = [7, 10, 14, 21, 28]     # ta.roc(low_1m, L)
ROCX_SMA_LENGTHS = [9, 13, 21, 34]         # ta.sma(close_45m, L)

ROCX_DEFAULTS = dict(
    rocNumBars=3, smaNumBars=2,
    vRoc=2,                                # index into ROCX_ROC_LENGTHS -> 14
    vSma=1,                                # index into ROCX_SMA_LENGTHS -> 13
    profitTargetPct=3.0 / 100, trailStopPct=2.0 / 100,
    leverage=1.0, enableLong=1.0,
    tslMode=0.0,          # 0 = pine close-follow trail (default), 1 = ratchet
)

# P-matrix column order (per-regime rows)
ROCX_PNAMES = ["pt", "tsl", "rocN", "smaN", "vRoc", "vSma", "lev", "eL", "tslm"]
_KEY2P = dict(pt="profitTargetPct", tsl="trailStopPct", rocN="rocNumBars",
              smaN="smaNumBars", vRoc="vRoc", vSma="vSma", lev="leverage",
              eL="enableLong", tslm="tslMode")


def rocx_P_from_dict(p):
    # missing keys fall back to defaults (older configs pre-date tslMode)
    return np.array([[float(p.get(_KEY2P[k], ROCX_DEFAULTS[_KEY2P[k]]))
                      for k in ROCX_PNAMES]])


def precompute_rocx(df3: pd.DataFrame, df1: pd.DataFrame) -> dict:
    t = df3["t"]
    c = df3["close"].to_numpy()
    n = len(c)
    # --- 1-min ROC(low) stack, sampled at 3-min closes ---
    low1 = df1["low"].to_numpy()
    end1 = (df1["t"] + pd.Timedelta(minutes=1)).to_numpy()
    end3 = (t + pd.Timedelta(minutes=3)).to_numpy()
    idx = np.searchsorted(end1, end3, side="right") - 1
    ok = idx >= 0
    roc_stack = np.full((len(ROCX_ROC_LENGTHS), n), np.nan)
    for vi, L in enumerate(ROCX_ROC_LENGTHS):
        r1 = np.full(len(low1), np.nan)
        if len(low1) > L:
            r1[L:] = (low1[L:] / low1[:-L] - 1.0) * 100.0
        roc_stack[vi, ok] = r1[idx[ok]]
    # --- 45-min SMA(close) stack (last COMPLETED bucket), sampled per bar ---
    tmin = (t.astype("int64") // 60_000_000_000).to_numpy()   # epoch minutes
    bidx = tmin // 45
    ub, last_pos = np.unique(bidx, return_index=True)
    # last 3-min bar of each bucket = position before the next bucket starts
    last_of_bucket = np.r_[last_pos[1:] - 1, n - 1]
    bclose = c[last_of_bucket]
    n_completed = np.searchsorted(ub, bidx, side="left")      # buckets fully closed
    csum = np.r_[0.0, np.cumsum(bclose)]
    sma_stack = np.full((len(ROCX_SMA_LENGTHS), n), np.nan)
    for vi, L in enumerate(ROCX_SMA_LENGTHS):
        m = n_completed >= L
        sma_stack[vi, m] = (csum[n_completed[m]] - csum[n_completed[m] - L]) / L
    is_new45 = np.zeros(n, dtype=np.int8)
    is_new45[0] = 1
    is_new45[1:] = (bidx[1:] != bidx[:-1]).astype(np.int8)
    t_ms = (t.astype("int64") // 10**6).to_numpy().astype(np.float64)
    return dict(t=t.to_numpy(), t_ms=t_ms,
                o=df3["open"].to_numpy(), h=df3["high"].to_numpy(),
                l=df3["low"].to_numpy(), c=c,
                roc_stack=roc_stack, sma_stack=sma_stack, is_new45=is_new45)


MAX_TRADES = 60000

@njit(cache=True)
def _core_rocx(o, h, l, c, roc_stack, sma_stack, is_new45,
               regime, P, warmup, initial_capital, commission, no_entry):
    """trade row: [entry_idx, exit_idx, dir, entry, exit, qty, net, mae,
    reason(0=TP, 1=trail stop, 2=LIQ), lev]"""
    n = len(c)
    eq = initial_capital
    pos = 0
    qty = 0.0
    entry_px = 0.0          # actual fill (for P&L)
    sig_entry_px = 0.0      # pine longEntryPrice (TP reference, reset by re-signals)
    trail = np.nan
    entry_i = -1
    mae = 0.0
    pend_entry = 0
    pend_qty = 0.0; pend_sig_px = 0.0; pend_trail = 0.0; pend_lev = 1.0
    pend_close = 0
    cur_lev = 1.0
    liquidated = 0
    out = np.empty((MAX_TRADES, 10))
    nt = 0

    for i in range(n):
        # ---- fills at this bar's open ----
        if pend_close != 0 and pos != 0:
            px = o[i]
            net = qty * (px - entry_px) - commission * qty * (entry_px + px)
            eq += net
            if nt < MAX_TRADES:
                out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = 1.0
                out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = pend_close - 1
                out[nt, 9] = cur_lev; nt += 1
            pos = 0; qty = 0.0; mae = 0.0
        if pend_entry != 0 and pos == 0:
            pos = 1; qty = pend_qty; entry_px = o[i]; entry_i = i
            sig_entry_px = pend_sig_px; trail = pend_trail
            cur_lev = pend_lev; mae = 0.0
        pend_entry = 0; pend_close = 0

        # ---- intrabar tracking / liquidation ----
        if pos != 0:
            adverse = l[i] / entry_px - 1.0
            if adverse < mae:
                mae = adverse
            if cur_lev > 1.0:
                liq_move = 1.0 / cur_lev - 0.008
                if adverse <= -liq_move:
                    px = entry_px * (1 - liq_move)
                    net = qty * (px - entry_px) - commission * qty * (entry_px + px)
                    eq += net
                    if nt < MAX_TRADES:
                        out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = 1.0
                        out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                        out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = 2.0
                        out[nt, 9] = cur_lev; nt += 1
                    pos = 0; qty = 0.0; mae = 0.0
                    liquidated = 1
                    break

        # ---- bar-close evaluation (pine order: entry block, trail, exits) ----
        r = regime[i]
        vR = int(P[r, 4]); vS = int(P[r, 5])
        rocN = int(P[r, 2]); smaN = int(P[r, 3])
        blocked = (i < warmup) or (no_entry[i] == 1)

        # ROC "trend": sampled series strictly falling toward the present
        roc_ok = True
        for k in range(1, rocN):
            ia = i - (k + 1); ib = i - k
            if ia < 0 or np.isnan(roc_stack[vR, ia]) or np.isnan(roc_stack[vR, ib]) \
                    or not (roc_stack[vR, ia] > roc_stack[vR, ib]):
                roc_ok = False
                break
        sma_ok = False
        if i - smaN >= 0 and not np.isnan(sma_stack[vS, i]) \
                and not np.isnan(sma_stack[vS, i - smaN]):
            sma_ok = sma_stack[vS, i] > sma_stack[vS, i - smaN]
        signal = (P[r, 7] > 0 and is_new45[i] == 1 and roc_ok and sma_ok)

        mark_eq = eq
        if pos != 0:
            mark_eq = eq + qty * (c[i] - entry_px)

        if signal and not blocked:
            if pos == 1:
                # rejected order, pine side effects: reset TP ref + LOOSEN trail
                # (tslm=1 ratchet: never loosen — only take the reset if higher)
                sig_entry_px = c[i]
                new_trail = c[i] * (1 - P[r, 1])
                if P[r, 8] <= 0 or new_trail > trail:
                    trail = new_trail
            else:
                pend_entry = 1
                pend_lev = P[r, 6]
                pend_qty = mark_eq * pend_lev / c[i]
                pend_sig_px = c[i]
                pend_trail = c[i] * (1 - P[r, 1])
        if pos == 1:
            # tslm=0 (pine-exact): the trail FOLLOWS the close in BOTH directions
            # while close > trail (NOT a from-the-high ratchet) — exits fire
            # only when one close drops trailPct below the previous close.
            # tslm=1 (ratchet, opt-in): trail only moves UP -> true high-water
            # trail + ~trailPct cap on loss from the signal close.
            new_trail = c[i] * (1 - P[r, 1])
            if P[r, 8] > 0:
                if new_trail > trail:
                    trail = new_trail
            elif c[i] > trail:
                trail = new_trail
            if c[i] >= sig_entry_px * (1 + P[r, 0]):
                pend_close = 1                        # take profit
            elif c[i] <= trail:
                pend_close = 2                        # trailing stop

    return out[:nt], eq, liquidated, pos, entry_px, qty, entry_i, cur_lev


def run_rocx_P(pre, P, regime=None, warmup=0, initial_capital=1000.0,
               commission=0.0, no_entry=None, return_open=False):
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    ne = no_entry if no_entry is not None else np.zeros(n, dtype=np.int8)
    arr, eq, liq, pos, epx, qty, ei, clev = _core_rocx(
        pre["o"], pre["h"], pre["l"], pre["c"],
        pre["roc_stack"], pre["sma_stack"], pre["is_new45"],
        np.asarray(regime, dtype=np.int32), np.asarray(P, dtype=np.float64),
        int(warmup), float(initial_capital), float(commission),
        np.asarray(ne, dtype=np.int8))
    t = pre["t"]
    tr = pd.DataFrame(arr, columns=["entry_idx", "exit_idx", "dir", "entry",
                                    "exit", "qty", "net", "mae", "reason", "lev"])
    if len(tr):
        tr["entry_t"] = [str(t[int(k)])[:16] for k in tr["entry_idx"]]
        tr["exit_t"] = [str(t[int(k)])[:16] for k in tr["exit_idx"]]
    else:
        tr["entry_t"] = []; tr["exit_t"] = []
    open_pos = None
    if pos != 0 and not liq:
        open_pos = dict(dir=1, entry=float(epx), qty=float(qty), lev=float(clev),
                        entry_t=str(t[int(ei)])[:16], entry_idx=int(ei))
    if return_open:
        return tr, float(eq), bool(liq), open_pos
    return tr, float(eq), bool(liq)


def run_rocx(pre, p, warmup=0, initial_capital=1000.0, commission=0.0,
             liq_threshold=None, no_entry=None, return_open=False):
    """Original-strategy runner: pine-named single parameter set."""
    return run_rocx_P(pre, rocx_P_from_dict(p), regime=None, warmup=warmup,
                      initial_capital=initial_capital, commission=commission,
                      no_entry=no_entry, return_open=return_open)
