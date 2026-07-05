#!/usr/bin/env python3
"""Live evaluator for V6 candidate configs (the wf2/optimizer format).

Consumes a candidate dict (zL/zS/zXS/zXLmax/ptScale/lev/eS3/eXS/sl/tv) plus a
regime method, builds the same P-matrix the backtest used, and evaluates the
exact engine conditions on the latest closed bar. Parity with the numba engine
is covered by test_parity_v6.py.
"""
import sys, os, json, logging
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))

from fast_engine import precompute, PARAM_NAMES          # noqa: E402
from adaptive import make_adaptive_pre                    # noqa: E402
from regimes import make_regimes                          # noqa: E402
from engine import DEFAULT_PARAMS                         # noqa: E402
from wf2 import build_P_v6, TREND_VARIANTS                # noqa: E402

logger = logging.getLogger(__name__)
I = {k: i for i, k in enumerate(PARAM_NAMES)}


class StrategyV6:
    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.cand = cfg["candidate"]
        self.mode = cfg["mode"]            # 'lev' (no stop) or 'spot'
        self.method = cfg.get("method", "none")
        self.state = state
        s = state
        for k in ["cdL_ms", "cdS_ms", "cdXS_ms", "cdXL_ms", "lastXL_ms", "lastXS_ms"]:
            s.setdefault(k, -1e18)
        s.setdefault("position", None)
        self._P = None
        self._R = None

    def P(self, R):
        if self._P is None or self._R != R:
            self._P = build_P_v6(self.cand, R)
            self._R = R
        return self._P

    def compute(self, df3, df1):
        pre = precompute(df3, df1, dict(DEFAULT_PARAMS))
        tz = TREND_VARIANTS[self.cand.get("tv", 0)]
        q, f = make_adaptive_pre(pre, trend_block_z=tz)
        regs, R = make_regimes(f, self.method)
        return q, regs, R

    def on_bar_close(self, df3, df1):
        q, regs, R = self.compute(df3, df1)
        return self.decide_at(q, regs, R, len(q["c"]) - 1)

    def decide_at(self, q, regs, R, i):
        base = DEFAULT_PARAMS
        if i < 300 or not np.isfinite(q["macdL"][i]) or not np.isfinite(q["xMacd"][i]):
            return []
        P = self.P(R)
        r = int(regs[i])
        tm = float(q["t_ms"][i])
        c = q["c"][i]
        s = self.state
        actions = []

        # cooldown updates (engine order)
        if q["cdMetricL"][i] <= -P[r, I["cdPctLong"]]: s["cdL_ms"] = tm
        if q["cdMetricS"][i] >= P[r, I["cdPctShort"]]: s["cdS_ms"] = tm
        if q["cdMetricXS"][i] >= P[r, I["xCdPctShort"]]: s["cdXS_ms"] = tm
        if q.get("cdMetricXL", q["cdMetricL"])[i] <= -P[r, I["xCdPctLong"]]: s["cdXL_ms"] = tm
        actL = (tm - s["cdL_ms"]) < P[r, I["cdPeriodLong"]] * 60000
        actS = (tm - s["cdS_ms"]) < P[r, I["cdPeriodShort"]] * 60000
        actXS = (tm - s["cdXS_ms"]) < P[r, I["xCdPeriodShort"]] * 60000
        actXL = (tm - s["cdXL_ms"]) < P[r, I["xCdPeriodLong"]] * 60000

        pos = s["position"]
        if pos is not None:
            # ---- exits (evaluated on bar close, like the engine) ----
            d = pos["dir"]
            rp = pos["regime"]  # PT params frozen at entry regime (engine uses live
            # regime row; we match engine: use CURRENT bar regime row)
            rr = r
            if self.mode == "spot":
                if d > 0 and q["l"][i] <= pos["sl_price"]:
                    return [dict(do="close", reason="stop_loss")]
                if d < 0 and q["h"][i] >= pos["sl_price"]:
                    return [dict(do="close", reason="stop_loss")]
            if pos["system"] == 0:  # 3m
                if d > 0:
                    pt, a1, a2 = P[rr, I["ptLong"]], P[rr, I["apt1Long"]], P[rr, I["apt2Long"]]
                    d1, d2 = P[rr, I["dur1Long"]], P[rr, I["dur2Long"]]
                else:
                    pt, a1, a2 = P[rr, I["ptShort"]], P[rr, I["apt1Short"]], P[rr, I["apt2Short"]]
                    d1, d2 = P[rr, I["dur1Short"]], P[rr, I["dur2Short"]]
            else:  # cross
                if d > 0:
                    pt, a1, a2 = P[rr, I["xTpLong"]], P[rr, I["xApt1Long"]], P[rr, I["xApt2Long"]]
                    d1, d2 = P[rr, I["xDur1Long"]], P[rr, I["xDur2Long"]]
                else:
                    pt, a1, a2 = P[rr, I["xTpShort"]], P[rr, I["xApt1Short"]], P[rr, I["xApt2Short"]]
                    d1, d2 = P[rr, I["xDur1Short"]], P[rr, I["xDur2Short"]]
            if tm >= pos["entry_sig_ms"] + d2 * 60000: pt = a2
            elif tm >= pos["entry_sig_ms"] + d1 * 60000: pt = a1
            if d > 0 and c >= pos["entry_price"] * (1 + pt):
                actions.append(dict(do="close", reason="profit_target"))
            elif d < 0 and c <= pos["entry_price"] * (1 - pt):
                actions.append(dict(do="close", reason="profit_target"))
            return actions

        # ---- entries ----
        long3m = (P[r, I["enableLong3m"]] > 0 and q["rsiL"][i] < P[r, I["rsiValLong"]]
                  and q["macdL"][i] < P[r, I["macdValPctLong"]] * c
                  and q["bbPctL"][i] < P[r, I["bbValLong"]]
                  and q["emaLongUp"][i] > 0 and not actL)
        short3m = (P[r, I["enableShort3m"]] > 0 and q["rsiL"][i] > P[r, I["rsiValShort"]]
                   and q["macdL"][i] > P[r, I["macdValPctShort"]] * c
                   and q["bbPctL"][i] > P[r, I["bbValShort"]]
                   and q["emaShortDown"][i] > 0 and not actS)
        gap_ms = P[r, I["xMinBetween"]] * 20 * 3 * 60000
        canL = (tm - s["lastXL_ms"]) > gap_ms
        canS = (tm - s["lastXS_ms"]) > gap_ms
        longX = (P[r, I["enableLongX"]] > 0 and q["xUp"][i] > 0
                 and q["xMacd"][i] < P[r, I["xMacdMaxLong"]]
                 and q["histRising"][i] > 0
                 and (P[r, I["requireHistPos"]] <= 0 or q["xHist"][i] > 0)
                 and not actXL and canL)
        shortX = (P[r, I["enableShortX"]] > 0 and q["xDn"][i] > 0
                  and q["xMacd"][i] > P[r, I["xMacdMinShort"]] and not actXS and canS)

        lev = float(P[r, I["leverage"]])
        open_act = None
        if long3m or longX:
            sys_ = 0 if long3m else 1
            if sys_ == 1: s["lastXL_ms"] = tm
            slp = c * (1 - (P[r, I["slLong"]] if sys_ == 0 else P[r, I["xSlLong"]]))
            open_act = dict(do="open", dir=1, system=sys_, regime=r, lev=lev,
                            sl_price=float(slp), sig_ms=tm, ref_close=float(c))
        if short3m or shortX:  # engine order: a short signal overrides the long
            sys_ = 0 if short3m else 1
            if sys_ == 1: s["lastXS_ms"] = tm
            slp = c * (1 + (P[r, I["slShort"]] if sys_ == 0 else P[r, I["xSlShort"]]))
            open_act = dict(do="open", dir=-1, system=sys_, regime=r, lev=lev,
                            sl_price=float(slp), sig_ms=tm, ref_close=float(c))
        if open_act:
            actions.append(open_act)
        return actions

    def intrabar_check(self, price: float):
        """In spot mode: intrabar protective stop (safer than backtest).
        In lev mode: liquidation-proximity warning + optional emergency exit."""
        pos = self.state["position"]
        if pos is None:
            return None
        d = pos["dir"]
        if self.mode == "spot":
            if d > 0 and price <= pos["sl_price"]:
                return dict(do="close", reason="stop_loss_intrabar")
            if d < 0 and price >= pos["sl_price"]:
                return dict(do="close", reason="stop_loss_intrabar")
            return None
        adverse = (price / pos["entry_price"] - 1.0) * d
        liq_dist = 1.0 / max(pos["lev"], 1e-9) - 0.008
        if adverse <= -0.5 * liq_dist:
            logger.warning("LIQUIDATION PROXIMITY: adverse %.2f%% of liq distance %.2f%%",
                           100 * -adverse, 100 * liq_dist)
        em = self.cfg.get("emergency_exit_adverse")
        if em and adverse <= -abs(em):
            return dict(do="close", reason="emergency_exit")
        return None
