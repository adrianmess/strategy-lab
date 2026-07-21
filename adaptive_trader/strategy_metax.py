#!/usr/bin/env python3
"""Live evaluator for MetaX ROUTER candidates.

Semantics (identical to the walk-forward-validated research sim):
  - every COMPONENT strategy runs its full virtual state machine over the
    feed window, exactly as its research engine would (the engine itself is
    executed — no re-ported logic, so component behavior is engine-exact);
  - a component's freshly-opened virtual trade becomes a REAL order only when
    the entry bar's market bucket is assigned to that component and the single
    position slot is free (first come, full equity);
  - the real position closes when the mirrored virtual trade closes.

Signal timing: engines decide at bar close and fill at next-bar open. To read
the decision at bar close (instead of one bar late), a SYNTHETIC zero-range
bar (o=h=l=c=last close) is appended before each evaluation: pending engine
fills land on it, surfacing "would open/close now" the moment the real bar
closes.

Buckets are computed live exactly as in research: causal rolling features
(e.g. vol3 = 30-day rolling percentile rank of 1-day realized vol, terciles at
0.33/0.67) — no frozen thresholds needed. Requires >= 31 days of history; the
feed backfills 35.

Supported component families: macdx, scalpx, scalpx2, v7/prime7, prime/v6 —
every family the router campaigns can produce.
"""
import sys, os, logging
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))

from regimes import regime_features, make_regimes          # noqa: E402

logger = logging.getLogger(__name__)
WARMUP = 3000
FUT_COMM, SPOT_COMM = 0.0004, 0.0005
BUCKET_METHOD = {"vol3": "vol3", "trend3": "trend3", "vt9": "volXtrend9",
                 "vol3_7d": "vol3_7d", "volume3": "volume3"}


def resolve_candidate(best_config, runs_dir):
    """Turn a metax best_config (which references components by run dir) into a
    self-contained trader candidate with every component's cand embedded.
    Shared by panel Adopt and the parity test."""
    import json
    cand = best_config["cand"]
    comps = []
    for c in cand["components"]:
        src = json.load(open(os.path.join(runs_dir, c["run"], c["file"])))
        comps.append(dict(strategy=c["strategy"],
                          method=src.get("method", "vol3"),
                          run=c["run"],
                          cand=(c.get("cand") or src["cand"])))
    return dict(strategy="metax", mode=best_config["mode"],
                buckets=cand["buckets"], assign=list(cand["assign"]),
                components=comps,
                source_run=best_config.get("generated"))


def _ts16(x):
    """Minute-resolution timestamp label, separator-normalized: numpy datetime64
    stringifies as '…T…', pandas as '… …' — they must compare equal."""
    return str(x)[:16].replace("T", " ")


def _extend(df3, df1):
    """Append the synthetic zero-range bar to both frames."""
    last3 = df3.iloc[-1]
    syn3 = dict(t=last3["t"] + pd.Timedelta(minutes=3), open=last3["close"],
                high=last3["close"], low=last3["close"], close=last3["close"],
                volume=0.0)
    last1 = df1.iloc[-1]
    syn1 = dict(t=last1["t"] + pd.Timedelta(minutes=1), open=last1["close"],
                high=last1["close"], low=last1["close"], close=last1["close"],
                volume=0.0)
    return (pd.concat([df3, pd.DataFrame([syn3])], ignore_index=True),
            pd.concat([df1, pd.DataFrame([syn1])], ignore_index=True))


class StrategyMetax:
    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.cand = cfg["candidate"]
        assert self.cand.get("strategy") == "metax", "not a router candidate"
        self.mode = cfg["mode"]
        self.buckets = self.cand["buckets"]
        self.assign = list(self.cand["assign"])
        self.comps = self.cand["components"]   # [{strategy, method, cand}, ...]
        for k in {a for a in self.assign if a is not None and a >= 0}:
            fam = self.comps[k]["strategy"]
            if fam not in ("macdx", "scalpx", "scalpx2", "v7", "prime7",
                           "prime", "v6"):
                raise SystemExit(f"metax live: assigned component family "
                                 f"'{fam}' has no live runner")
        self.state = state
        state.setdefault("position", None)
        # mirror = the component virtual trade the real position is tracking:
        # dict(comp=k, entry_t="YYYY-mm-dd HH:MM", dir=±1, lev=x)
        state.setdefault("mirror", None)

    # ---------------- component virtual runs (engine-exact) ----------------
    def _features(self, df3):
        pre = dict(c=df3["close"].to_numpy(), h=df3["high"].to_numpy(),
                   l=df3["low"].to_numpy(), vol=df3["volume"].to_numpy())
        return regime_features(pre)

    def _run_component(self, comp, df3, df1, feats):
        """Run one component's engine over the (synthetic-extended) window.
        Returns (trades_df, open_pos_or_None)."""
        strat, cand = comp["strategy"], comp["cand"]
        method = comp.get("method", "vol3")
        reg, R = make_regimes(feats, method)
        warmup = min(WARMUP, max(0, len(df3) - 200))
        comm = FUT_COMM if self.mode == "lev" else SPOT_COMM
        if strat == "macdx":
            from macdx_engine import precompute_macdx, run_macdx_P, MACDX_DEFAULTS
            from wf2 import build_P_macdx
            pre = precompute_macdx(df3, df1, MACDX_DEFAULTS)
            P = build_P_macdx(cand, R)
            tr, eq, liq, op = run_macdx_P(pre, P, regime=reg, warmup=warmup,
                                          initial_capital=1000.0,
                                          commission=comm, return_open=True)
            return tr, op
        if strat == "scalpx":
            from scalp_engine import scalp_precompute, run_scalp
            from wf2 import build_P_scalpx
            pre = scalp_precompute(df3)
            P = build_P_scalpx(cand, R)
            tr, eq, liq, op = run_scalp(pre, P, regime=reg, warmup=warmup,
                                        initial_capital=1000.0, commission=comm,
                                        liq_threshold=(-1.0 if self.mode == "lev"
                                                       else 1e9),
                                        return_open=True)
            return tr, op
        if strat == "scalpx2":
            from scalp_engine import scalp_precompute2, run_scalp2
            from wf2 import build_P_scalpx2
            pre = scalp_precompute2(df3)
            P, vidx = build_P_scalpx2(cand, R)
            tr, eq, liq, op = run_scalp2(pre, P, vidx, regime=reg, warmup=warmup,
                                         initial_capital=1000.0, commission=comm,
                                         liq_threshold=(-1.0 if self.mode == "lev"
                                                        else 1e9),
                                         return_open=True)
            return tr, op
        if strat in ("v7", "prime7"):
            from engine3 import precompute3, run3
            from optimizer2 import build_P3
            pre = precompute3(df3, df1)
            P = build_P3(cand)
            if P.shape[0] != R:
                P = np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
            use_sl = (self.mode == "spot") or bool(cand.get("lev_stops"))
            tr, eq, liq, op = run3(pre, P, regime=reg, warmup=warmup,
                                   initial_capital=1000.0, commission=comm,
                                   use_sl=use_sl, dyn_liq=(self.mode == "lev"),
                                   return_open=True)
            return tr, op
        if strat in ("prime", "v6"):
            from fast_engine import precompute, run_fast
            from adaptive import make_adaptive_pre
            from wf2 import build_P_prime, build_P_v6, TREND_VARIANTS
            pre0 = precompute(df3, df1)
            tv = int(cand.get("tv", 0) or 0)
            q, _f = make_adaptive_pre(pre0, trend_block_z=TREND_VARIANTS[tv])
            P = (build_P_prime if strat == "prime" else build_P_v6)(cand, R)
            use_sl = (self.mode == "spot") or bool(cand.get("lev_stops"))
            tr, eq, liq, op = run_fast(q, P, regime=reg, warmup=warmup,
                                       initial_capital=1000.0, commission=comm,
                                       use_sl=use_sl,
                                       liq_threshold=(-1.0 if self.mode == "lev"
                                                      else 1e9),
                                       return_open=True)
            return tr, op
        raise SystemExit(f"unsupported component family {strat}")

    # ---------------- router ----------------
    def on_bar_close(self, df3, df1):
        if len(df3) < WARMUP + 300:
            logger.warning("metax: window too short (%d bars)", len(df3))
            return []
        x3, x1 = _extend(df3, df1)
        feats = self._features(x3)
        breg, _ = make_regimes(feats, BUCKET_METHOD[self.buckets])
        bucket_now = int(breg[len(x3) - 2])     # last REAL bar = the signal bar
        syn_i = len(x3) - 1
        syn_t = _ts16(x3["t"].iloc[-1])
        s = self.state
        mirror = s.get("mirror")

        runs = {}
        def get_run(k):
            if k not in runs:
                runs[k] = self._run_component(self.comps[k], x3, x1, feats)
            return runs[k]

        # 1) does the mirrored virtual trade close now (or already)?
        if mirror is not None:
            k = mirror["comp"]
            tr, op = get_run(k)
            still_open = (op is not None
                          and _ts16(op.get("entry_t", "")) == mirror["entry_t"])
            if not still_open:
                s["mirror"] = None
                reason = "virtual_exit"
                if len(tr):
                    m = tr[tr["entry_t"].map(_ts16) == mirror["entry_t"]]
                    if len(m):
                        rmap = {0.0: "profit_target", 1.0: "stop_loss",
                                2.0: "liquidation"}
                        reason = rmap.get(float(m.iloc[-1]["reason"]),
                                          str(m.iloc[-1]["reason"]))
                logger.info("metax: component %d virtual trade closed (%s)",
                            k, reason)
                return [dict(do="close", reason=f"router:{reason}")]
            return []   # mirrored trade still open — nothing else may fire

        # 2) slot free: does the bucket owner open a virtual trade now?
        k = self.assign[bucket_now] if bucket_now < len(self.assign) else -1
        if k is None or k < 0:
            return []
        tr, op = get_run(k)
        opens_now = (op is not None
                     and int(op.get("entry_idx", -1)) == syn_i)
        if not opens_now:
            return []
        d = int(np.sign(op.get("dir", 1))) or 1
        lev = float(op.get("lev", 1.0)) if self.mode == "lev" else 1.0
        c = float(df3["close"].iloc[-1])
        s["mirror"] = dict(comp=k, entry_t=syn_t, dir=d, lev=lev)
        logger.info("metax: bucket %d -> component %d (%s) OPEN dir=%+d lev=%.0f",
                    bucket_now, k, self.comps[k]["strategy"], d, lev)
        return [dict(do="open", dir=d, system=0, regime=bucket_now, lev=lev,
                     sl_price=0.0, sig_ms=float(x3["t"].iloc[-1].value // 10**6),
                     ref_close=c)]

    # ---------------- intra-bar ----------------
    def intrabar_check(self, price: float):
        """Component exits are engine-timed (mirrored at bar close). This only
        enforces the global emergency net + liq-proximity warning on lev."""
        pos = self.state["position"]
        if pos is None:
            return None
        d = pos["dir"]
        adverse = (price / pos["entry_price"] - 1.0) * d
        if self.mode == "lev":
            liq_dist = 1.0 / max(pos["lev"], 1e-9) - 0.008
            if adverse <= -0.5 * liq_dist:
                logger.warning("LIQUIDATION PROXIMITY: adverse %.2f%%, liq at "
                               "%.2f%%", 100 * -adverse, 100 * liq_dist)
        em = self.cfg.get("emergency_exit_adverse")
        if em and adverse <= -abs(em):
            self.state["mirror"] = None     # stop tracking the virtual trade
            return dict(do="close", reason="emergency_exit")
        return None
