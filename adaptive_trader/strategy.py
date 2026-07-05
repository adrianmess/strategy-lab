#!/usr/bin/env python3
"""Live signal evaluator.

Reuses the exact indicator/regime code from research/ (same math that was
validated bar-for-bar against TradingView), then applies the entry/exit rules
to the latest closed bar with persistent trading state.

Semantics match the backtest: decisions on 3m bar close, orders at market
immediately after (backtest fills at next bar open). One improvement over the
backtest: the protective stop is also checked intra-bar between closes, which
can only exit earlier (smaller loss) than the backtest assumed.
"""
import sys, os, json, logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "research"))
from fast_engine import precompute            # noqa: E402
from adaptive import make_adaptive_pre        # noqa: E402
from regimes import vol_terciles              # noqa: E402
from engine import DEFAULT_PARAMS             # noqa: E402

logger = logging.getLogger(__name__)
TREND_VARIANTS = [None, 1.5, 2.0, 3.0]


class Strategy:
    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.p = cfg["params"]
        self.state = state  # persisted dict, mutated in place
        s = state
        s.setdefault("cdL_ms", -1e18)      # cooldown start times (bar open, ms)
        s.setdefault("cdS_ms", -1e18)
        s.setdefault("cdXS_ms", -1e18)
        s.setdefault("lastXL_ms", -1e18)   # last cross-entry signal bar times
        s.setdefault("lastXS_ms", -1e18)
        s.setdefault("position", None)

    # ---------- indicator layer (identical code path to backtest) ----------
    def compute(self, df3: pd.DataFrame, df1: pd.DataFrame):
        base = dict(DEFAULT_PARAMS)
        pre = precompute(df3, df1, base)
        tz = TREND_VARIANTS[self.p.get("tv", 0)]
        q, f = make_adaptive_pre(pre, trend_block_z=tz)
        reg = vol_terciles(f["volPct"])
        return q, f, reg

    # ---------- per-regime parameter helpers ----------
    def regime_params(self, r: int):
        p, base = self.p, DEFAULT_PARAMS
        dial = float(self.cfg.get("risk_dial", 1.0))
        lev = min(p["lev"][r] * dial, 5.0)
        ps = p["ptScale"][r]
        return dict(
            zL=p["zL"][r], zS=p["zS"][r], zXS=p["zXS"][r],
            lev=lev, sl=p["sl"],
            ptLong=base["ptLong"] * ps, apt1Long=base["apt1Long"] * ps,
            apt2Long=base["apt2Long"] * ps,
            ptShort=base["ptShort"] * ps, apt1Short=base["apt1Short"] * ps,
            apt2Short=base["apt2Short"] * ps,
            xTpLong=base["xTpLong"] * ps, xApt1Long=base["xApt1Long"] * ps,
            xApt2Long=base["xApt2Long"] * ps,
            xTpShort=base["xTpShort"] * ps, xApt1Short=base["xApt1Short"] * ps,
            xApt2Short=base["xApt2Short"] * ps,
        )

    # ---------- decision on the latest closed bar ----------
    def on_bar_close(self, df3, df1):
        """Returns list of actions: dicts like {'do':'open'|'close', ...}."""
        q, f, regs = self.compute(df3, df1)
        return self.decide_at(q, regs, len(q["c"]) - 1)

    def decide_at(self, q, regs, i):
        """Evaluate rules at bar index i of precomputed arrays (used live for
        i=last; used by tests to replay history bar-by-bar)."""
        base = DEFAULT_PARAMS
        if i < 300 or not np.isfinite(q["macdL"][i]) or not np.isfinite(q["xMacd"][i]):
            return []
        r = int(regs[i])
        rp = self.regime_params(r)
        tm = float(q["t_ms"][i])
        c = q["c"][i]
        s = self.state
        actions = []

        # cooldown state updates (same order as Pine: before entries)
        if q["cdMetricL"][i] <= -base["cdPctLong"]:
            s["cdL_ms"] = tm
        if q["cdMetricS"][i] >= base["cdPctShort"]:
            s["cdS_ms"] = tm
        if q["cdMetricXS"][i] >= base["xCdPctShort"]:
            s["cdXS_ms"] = tm
        actL = (tm - s["cdL_ms"]) < base["cdPeriodLong"] * 60000
        actS = (tm - s["cdS_ms"]) < base["cdPeriodShort"] * 60000
        actXS = (tm - s["cdXS_ms"]) < base["xCdPeriodShort"] * 60000

        pos = s["position"]

        # ---------- exits ----------
        if pos is not None:
            d = pos["dir"]
            if d > 0 and q["l"][i] <= pos["sl_price"]:
                actions.append(dict(do="close", reason="stop_loss"))
            elif d < 0 and q["h"][i] >= pos["sl_price"]:
                actions.append(dict(do="close", reason="stop_loss"))
            else:
                rp_e = self.regime_params(pos["regime"])  # PT params fixed at entry regime
                if pos["system"] == "3m":
                    if d > 0:
                        pt, a1, a2 = rp_e["ptLong"], rp_e["apt1Long"], rp_e["apt2Long"]
                    else:
                        pt, a1, a2 = rp_e["ptShort"], rp_e["apt1Short"], rp_e["apt2Short"]
                    d1, d2 = base["dur1Long"], base["dur2Long"]
                else:
                    if d > 0:
                        pt, a1, a2 = rp_e["xTpLong"], rp_e["xApt1Long"], rp_e["xApt2Long"]
                        d1, d2 = base["xDur1Long"], base["xDur2Long"]
                    else:
                        pt, a1, a2 = rp_e["xTpShort"], rp_e["xApt1Short"], rp_e["xApt2Short"]
                        d1, d2 = base["xDur1Short"], base["xDur2Short"]
                if tm >= pos["entry_sig_ms"] + d2 * 60000:
                    pt = a2
                elif tm >= pos["entry_sig_ms"] + d1 * 60000:
                    pt = a1
                if d > 0 and c >= pos["entry_price"] * (1 + pt):
                    actions.append(dict(do="close", reason="profit_target"))
                elif d < 0 and c <= pos["entry_price"] * (1 - pt):
                    actions.append(dict(do="close", reason="profit_target"))
            return actions  # never enter on the same bar we're still in a position

        # ---------- entries ----------
        long3m = (q["rsiL"][i] < base["rsiValLong"]
                  and q["macdL"][i] < rp["zL"] * c        # z-scored macd (see adaptive.py)
                  and q["bbPctL"][i] < base["bbValLong"]
                  and q["emaLongUp"][i] > 0 and not actL)
        short3m = (q["rsiL"][i] > base["rsiValShort"]
                   and q["macdL"][i] > rp["zS"] * c
                   and q["bbPctL"][i] > base["bbValShort"]
                   and q["emaShortDown"][i] > 0 and not actS)
        gap_ms = base["xMinBetween"] * 20 * 3 * 60000  # bars->ms
        canL = (tm - s["lastXL_ms"]) > gap_ms
        canS = (tm - s["lastXS_ms"]) > gap_ms
        longX = (q["xUp"][i] > 0 and q["xMacd"][i] < base["xMacdMaxLong"]
                 and q["histRising"][i] > 0 and q["xHist"][i] > 0 and canL)
        shortX = (q["xDn"][i] > 0 and q["xMacd"][i] > rp["zXS"] and not actXS and canS)

        if long3m or longX:
            system = "3m" if long3m else "3m_cross"
            if system == "3m_cross":
                s["lastXL_ms"] = tm
            actions.append(dict(do="open", dir=1, system=system, regime=r,
                                lev=rp["lev"], sl=rp["sl"],
                                sl_price=c * (1 - rp["sl"]), sig_ms=tm, ref_close=c))
        elif short3m or shortX:
            system = "3m" if short3m else "3m_cross"
            if system == "3m_cross":
                s["lastXS_ms"] = tm
            actions.append(dict(do="open", dir=-1, system=system, regime=r,
                                lev=rp["lev"], sl=rp["sl"],
                                sl_price=c * (1 + rp["sl"]), sig_ms=tm, ref_close=c))
        return actions

    # ---------- intra-bar protective check (safer than backtest) ----------
    def intrabar_stop(self, price: float):
        pos = self.state["position"]
        if pos is None:
            return None
        if pos["dir"] > 0 and price <= pos["sl_price"]:
            return dict(do="close", reason="stop_loss_intrabar")
        if pos["dir"] < 0 and price >= pos["sl_price"]:
            return dict(do="close", reason="stop_loss_intrabar")
        return None
