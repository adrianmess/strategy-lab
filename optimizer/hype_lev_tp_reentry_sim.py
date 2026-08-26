#!/usr/bin/env python3
"""HYPE LEV intra-trade take-profit + re-entry study (research only).

Adrian's observation: a HYPE lev position ran past +5% margin, then sagged
to -3%. Could a rule bank the +5% and re-enter lower, instead of holding to
the component's mirror exit?

Frame: for every historical virtual trade of the LEV router's HYPE
components, walk the 1m price path from entry to exit and apply
  TP X      close when OUR unrealized (margin-basis) reaches +X%
  re-entry  dipD  — re-enter when price retraces D% (of price) below the
                    TP exit, direction-aware (the live anti-churn shape)
            virt0 — re-enter when price is back at/below the VIRTUAL entry
                    (u_virtual <= 0), Adrian's "went to 0 or less"
  fallback  if still in at the sim's exit bar, exit there (mirror exit);
            if flat at the end, the banked profit stands
  TRAIL T/dipD variant: exit when unrealized falls T margin-points off its
  intra-trade peak (a "it's falling" detector with no foresight), same
  re-entry
Each leg pays a round trip of 2 x fee/side x lev (margin-basis). Two fee
regimes: engine parity (0.04%/side) and today's actual HYPE futures promo
(0%/side) — churn is free under the promo, so both are shown.

Baseline for honesty: hold entry->exit recomputed from the same 1m closes
(so policy vs baseline differ only by the policy).
"""
import glob
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "runs", "All_pairs_LEV_1m-3m_multi-strat")
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
NS_MIN = 60_000_000_000


def main():
    df = pd.read_parquet(os.path.join(DATA, "hype_1min.parquet"))
    tc = [c for c in df.columns
          if c.lower() in ("t", "time", "ts", "datetime", "date")][0]
    df[tc] = pd.to_datetime(df[tc])
    s = df.set_index(tc)["close"]
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    ts, px = s.index.asi8, s.values.astype(np.float64)

    comps = []
    for f in sorted(glob.glob(os.path.join(RUN, "_t_hype_*.json"))):
        for run, tab in json.load(open(f)).items():
            tr = tab.get("trades") or []
            if not tr:
                continue
            et = np.array([x[0] for x in tr], dtype=np.int64)
            xt = np.array([x[1] for x in tr], dtype=np.int64)
            r = np.array([x[2] for x in tr])
            ei = np.clip(np.searchsorted(ts, et, side="right") - 1, 0, None)
            xi = np.clip(np.searchsorted(ts, xt, side="right") - 1, 0, None)
            epx, xpx = px[ei], px[xi]
            pm = xpx / np.maximum(epx, 1e-12) - 1.0
            dirs = np.where(r * pm >= 0, 1, -1)
            good = np.abs(pm) > 5e-4
            lev = (float(np.median(np.abs(r[good]) / np.abs(pm[good])))
                   if good.sum() >= 3 else 1.0)
            lev = float(np.clip(round(lev, 1), 1.0, 100.0))
            comps.append(dict(run=run, et=et, xt=xt, ei=ei, xi=xi,
                              epx=epx, xpx=xpx, dir=dirs, lev=lev,
                              n=len(tr)))
            print(f"{run}: {len(tr)} trades, lev~{lev}x, "
                  f"{int((dirs<0).sum())} short")

    def run_policy(c, k, tp, rule, dparm, fee_side, trail=None):
        """One trade's legs under the policy -> (mult, cycles)."""
        d, lv = int(c["dir"][k]), c["lev"]
        fee = 2.0 * fee_side * lv
        i0, i1 = int(c["ei"][k]), int(c["xi"][k])
        epx_v = c["epx"][k]
        mult, cycles = 1.0, 0
        in_pos, entry = True, epx_v
        exit_px = None
        peak = 0.0
        for i in range(i0 + 1, i1 + 1):
            p = px[i]
            if in_pos:
                u = (p / entry - 1.0) * d * lv
                peak = max(peak, u)
                fire = (u >= tp) if trail is None \
                    else (peak >= tp and peak - u >= trail)
                if fire:
                    mult *= 1.0 + u - fee
                    in_pos, exit_px, peak = False, p, 0.0
                    cycles += 1
            else:
                if rule == "dip":
                    back = (exit_px - p) * d >= dparm / 100.0 * exit_px
                else:                      # virt0
                    back = (p / epx_v - 1.0) * d <= 0.0
                if back:
                    in_pos, entry = True, p
        if in_pos:
            mult *= 1.0 + (c["xpx"][k] / entry - 1.0) * d * lv - fee
        return mult, cycles

    for fee_side, fl in ((0.0004, "engine-parity 0.04%/side"),
                        (0.0, "current HYPE promo 0%/side")):
        print(f"\n================ fees: {fl} ================")
        for c in comps:
            base = 1.0
            for k in range(c["n"]):
                d, lv = int(c["dir"][k]), c["lev"]
                base *= max(1e-9, 1.0 + (c["xpx"][k] / c["epx"][k] - 1.0)
                            * d * lv - 2.0 * fee_side * lv)
            print(f"\n  {c['run']} — baseline hold-to-exit: x{base:.2f}")
            pols = ([("TP +%g, re-enter dip %g%%" % (100 * tp, dd),
                      tp, "dip", dd, None)
                     for tp in (0.03, 0.05, 0.08, 0.12)
                     for dd in (0.5, 1.0, 2.0)]
                    + [("TP +%g, re-enter at virtual 0" % (100 * tp),
                        tp, "virt0", 0, None)
                       for tp in (0.03, 0.05, 0.08, 0.12)]
                    + [("TRAIL %g off peak (arm at +%g), re-enter dip 1%%"
                        % (100 * tr_, 100 * tp), tp, "dip", 1.0, tr_)
                       for tp, tr_ in ((0.03, 0.03), (0.05, 0.03),
                                       (0.05, 0.05))])
            rows = []
            for label, tp, rule, dd, trail in pols:
                mult, cyc, better = 1.0, 0, 0
                for k in range(c["n"]):
                    m, cy = run_policy(c, k, tp, rule, dd, fee_side, trail)
                    d, lv = int(c["dir"][k]), c["lev"]
                    b = (1.0 + (c["xpx"][k] / c["epx"][k] - 1.0) * d * lv
                         - 2.0 * fee_side * lv)
                    mult *= max(m, 1e-9)
                    cyc += cy
                    better += m > b + 1e-12
                rows.append((mult, label, cyc, better))
            rows.sort(reverse=True)
            for mult, label, cyc, better in rows:
                tag = "BEATS HOLD" if mult > base else "worse"
                print(f"    x{mult:9.2f} ({tag:>10}) {label} "
                      f"[{cyc} extra cycles, better on {better}/{c['n']} "
                      f"trades]")


if __name__ == "__main__":
    main()
