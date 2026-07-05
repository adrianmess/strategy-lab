#!/usr/bin/env python3
"""Live evaluator for V7 candidates (engine3: full-parameter, per-regime,
searchable indicator lengths). Mirrors engine3._core3 bar-close semantics;
parity is covered by test_parity_v7.py."""
import sys, os, logging
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))

from engine3 import precompute3, vec3, C, VARIANTS       # noqa: E402
from regimes import make_regimes                          # noqa: E402

logger = logging.getLogger(__name__)
NHIST = len(VARIANTS["histn"])


class StrategyV7:
    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.cand = cfg["candidate"]
        self.mode = cfg["mode"]
        self.method = cfg.get("method", "vol3")
        self.state = state
        s = state
        for k in ["cdL_ms", "cdS_ms", "cdXL_ms", "cdXS_ms", "lastXL_ms", "lastXS_ms"]:
            s.setdefault(k, -1e18)
        s.setdefault("position", None)
        self.P = np.vstack([vec3(reg) for reg in self.cand["regs"]])

    def compute(self, df3, df1):
        pre = precompute3(df3, df1)
        regs, R = make_regimes(pre["feats"], self.method)
        P = self.P
        if P.shape[0] != R:
            P = np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
        return pre, regs, P

    def on_bar_close(self, df3, df1):
        pre, regs, P = self.compute(df3, df1)
        return self.decide_at(pre, regs, P, len(pre["c"]) - 1)

    def decide_at(self, q, regs, P, i):
        if i < 300:
            return []
        r = int(regs[i])
        tm = float(q["t_ms"][i])
        c = q["c"][i]
        s = self.state
        # cooldowns (engine3 order)
        if q["cd1"][i] <= -P[r, C["cdPctLong"]]: s["cdL_ms"] = tm
        if -q["cd3"][i] >= P[r, C["cdPctShort"]]: s["cdS_ms"] = tm
        if q["cd1"][i] <= -P[r, C["xCdPctLong"]]: s["cdXL_ms"] = tm
        if -q["cd3"][i] >= P[r, C["xCdPctShort"]]: s["cdXS_ms"] = tm
        actL = (tm - s["cdL_ms"]) < P[r, C["cdPeriodLong"]] * 60000
        actS = (tm - s["cdS_ms"]) < P[r, C["cdPeriodShort"]] * 60000
        actXL = (tm - s["cdXL_ms"]) < P[r, C["xCdPeriodLong"]] * 60000
        actXS = (tm - s["cdXS_ms"]) < P[r, C["xCdPeriodShort"]] * 60000

        vr = int(P[r, C["vRsi"]]); vm = int(P[r, C["vMacd"]]); vb = int(P[r, C["vBB"]])
        vu = int(P[r, C["vEmaUp"]]); vd = int(P[r, C["vEmaDn"]])
        vx = int(P[r, C["vX"]]); vh = int(P[r, C["vHistN"]])
        rsiv = q["rsi_all"][vr, i]; mz = q["macdz_all"][vm, i]
        bbv = q["bb_all"][vb, i]; xz = q["xz_all"][vx, i]
        if not (np.isfinite(mz) and np.isfinite(xz)):
            return []

        pos = s["position"]
        if pos is not None:
            d = pos["dir"]
            if self.mode == "spot":
                if d > 0 and q["l"][i] <= pos["sl_price"]:
                    return [dict(do="close", reason="stop_loss")]
                if d < 0 and q["h"][i] >= pos["sl_price"]:
                    return [dict(do="close", reason="stop_loss")]
            if pos["system"] == 0:
                if d > 0:
                    pt, a1, a2 = P[r, C["ptLong"]], P[r, C["apt1Long"]], P[r, C["apt2Long"]]
                    d1, d2 = P[r, C["dur1Long"]], P[r, C["dur2Long"]]
                else:
                    pt, a1, a2 = P[r, C["ptShort"]], P[r, C["apt1Short"]], P[r, C["apt2Short"]]
                    d1, d2 = P[r, C["dur1Short"]], P[r, C["dur2Short"]]
            else:
                if d > 0:
                    pt, a1, a2 = P[r, C["xTpLong"]], P[r, C["xApt1Long"]], P[r, C["xApt2Long"]]
                    d1, d2 = P[r, C["xDur1Long"]], P[r, C["xDur2Long"]]
                else:
                    pt, a1, a2 = P[r, C["xTpShort"]], P[r, C["xApt1Short"]], P[r, C["xApt2Short"]]
                    d1, d2 = P[r, C["xDur1Short"]], P[r, C["xDur2Short"]]
            if tm >= pos["entry_sig_ms"] + d2 * 60000: pt = a2
            elif tm >= pos["entry_sig_ms"] + d1 * 60000: pt = a1
            if d > 0 and c >= pos["entry_price"] * (1 + pt):
                return [dict(do="close", reason="profit_target")]
            if d < 0 and c <= pos["entry_price"] * (1 - pt):
                return [dict(do="close", reason="profit_target")]
            return []

        blockShort = q["trend"][i] > P[r, C["trendBlockZ"]]
        long3m = (P[r, C["eL3"]] > 0 and rsiv < P[r, C["rsiValLong"]]
                  and mz < P[r, C["zL"]] and bbv < P[r, C["bbValLong"]]
                  and q["emaup_all"][vu, i] > 0 and not actL)
        short3m = (P[r, C["eS3"]] > 0 and rsiv > P[r, C["rsiValShort"]]
                   and mz > P[r, C["zS"]] and bbv > P[r, C["bbValShort"]]
                   and q["emadn_all"][vd, i] > 0 and not actS and not blockShort)
        gap_ms = P[r, C["xMinBetween"]] * 20 * 3 * 60000
        canL = (tm - s["lastXL_ms"]) > gap_ms
        canS = (tm - s["lastXS_ms"]) > gap_ms
        hist_row = vx * NHIST + vh
        longX = (P[r, C["eXL"]] > 0 and q["xup_all"][vx, i] > 0
                 and xz < P[r, C["zXLmax"]] and q["hist_all"][hist_row, i] > 0
                 and (P[r, C["requireHistPos"]] <= 0 or q["xhist_all"][vx, i] > 0)
                 and not actXL and canL)
        shortX = (P[r, C["eXS"]] > 0 and q["xdn_all"][vx, i] > 0
                  and xz > P[r, C["zXS"]] and not actXS and canS and not blockShort)

        lev = float(P[r, C["leverage"]])
        act = None
        if long3m or longX:
            sys_ = 0 if long3m else 1
            if sys_ == 1: s["lastXL_ms"] = tm
            act = dict(do="open", dir=1, system=sys_, regime=r, lev=lev,
                       sl_price=float(c * (1 - P[r, C["slLong"]])), sig_ms=tm,
                       ref_close=float(c))
        if short3m or shortX:
            sys_ = 0 if short3m else 1
            if sys_ == 1: s["lastXS_ms"] = tm
            act = dict(do="open", dir=-1, system=sys_, regime=r, lev=lev,
                       sl_price=float(c * (1 + P[r, C["slShort"]])), sig_ms=tm,
                       ref_close=float(c))
        return [act] if act else []

    def intrabar_check(self, price: float):
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
            logger.warning("LIQUIDATION PROXIMITY: adverse %.2f%%, liq at %.2f%%",
                           100 * -adverse, 100 * liq_dist)
        em = self.cfg.get("emergency_exit_adverse")
        if em and adverse <= -abs(em):
            return dict(do="close", reason="emergency_exit")
        return None
