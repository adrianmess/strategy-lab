#!/usr/bin/env python3
"""Assemble all results into dashboard/data.js for the static site."""
import json, os, glob
import numpy as np
import pandas as pd

OUT = "dashboard_data.json"

def baselines():
    """Original strategies, original parameters, on CoinAPI data."""
    import pickle
    from fast_engine import params_to_vec, run_fast
    from scalp_engine import scalp_vec, run_scalp
    from engine import DEFAULT_PARAMS
    from wf2 import mtm_curve
    pres = pickle.load(open("precomputed.pkl", "rb"))          # raw V5.2 pres
    sc = pickle.load(open("scalp_pre.pkl", "rb"))
    out = []
    def run_v5(use_sl, label):
        eq = 1000.0; trs = []; mdd = 0.0; liq_any = False; curve = []
        P = params_to_vec(DEFAULT_PARAMS)
        for pre in pres:
            eq0 = eq
            tr, eq, liq = run_fast(pre, P, warmup=3000, initial_capital=eq,
                                   use_sl=use_sl, liq_threshold=0.117)
            trs.append(tr)
            if len(tr):
                m, d = mtm_curve(tr, pre["c"], initial=eq0)
                mdd = max(mdd, d)
                step = max(1, len(m) // 300)
                for x, v in zip(pd.to_datetime(pre["t"][::step]), m[::step]):
                    if np.isfinite(v): curve.append(dict(t=str(x), eq=float(v)))
            if liq: liq_any = True; break
        tr = pd.concat(trs, ignore_index=True)
        months = 25.3
        g = np.log(max(eq, 1e-9) / 1000) / months
        out.append(dict(cid=label, months=months, final_eq=float(eq),
                        total_mult=eq / 1000, monthly_growth_pct=(np.exp(g) - 1) * 100,
                        liq=liq_any, maxdd_mtm=mdd, n=len(tr), tpm=len(tr) / months,
                        sl_hits=int((tr["reason"] == 1).sum()),
                        worst_mae=float(tr["mae"].min()), win=float((tr["net"] > 0).mean()),
                        curve=curve, baseline=True))
    run_v5(True, "BASELINE: V5 original, 8x, stop-loss ON")
    run_v5(False, "BASELINE: V5 original, 8x, stop OFF (your live config)")
    # scalp default, 1x
    eq = 100.0; trs = []; mdd = 0.0; curve = []
    P = scalp_vec()
    for pre, f in sc:
        eq0 = eq
        tr, eq, liq = run_scalp(pre, P, warmup=2500, initial_capital=eq)
        trs.append(tr)
        if len(tr):
            m, d = mtm_curve(tr, pre["c"], initial=eq0)
            mdd = max(mdd, d)
            step = max(1, len(m) // 300)
            for x, v in zip(pd.to_datetime(pre["t"][::step]), m[::step]):
                if np.isfinite(v): curve.append(dict(t=str(x), eq=float(v)))
    tr = pd.concat(trs, ignore_index=True)
    g = np.log(max(eq, 1e-9) / 100) / 25.3
    out.append(dict(cid="BASELINE: Scalp original, 1x", months=25.3, final_eq=float(eq),
                    total_mult=eq / 100, monthly_growth_pct=(np.exp(g) - 1) * 100,
                    liq=False, maxdd_mtm=mdd, n=len(tr), tpm=len(tr) / 25.3,
                    sl_hits=int((tr["reason"] == 1).sum()),
                    worst_mae=float(tr["mae"].min()), win=float((tr["net"] > 0).mean()),
                    curve=curve, baseline=True))
    return out

def main():
    resim = []
    for f in ["resim_part1.json", "resim_part2.json", "resim_part3.json"]:
        if os.path.exists(f):
            resim.extend(json.load(open(f)))
    # fold tables
    folds = {}
    for cid_dir in sorted(glob.glob("wf2_results/*")):
        cid = os.path.basename(cid_dir)
        rows = []
        for p in sorted(glob.glob(os.path.join(cid_dir, "fold_*.json"))):
            r = json.load(open(p))
            row = dict(fold=r["job"]["fold_idx"], status=r["status"])
            if r["status"] == "ok":
                row.update(train_growth=r["train"]["growth"],
                           n_feasible=r.get("n_feasible"))
                if r.get("oos"):
                    o = r["oos"]
                    row.update(oos_growth=o["growth"], oos_n=o["n"],
                               oos_dd=o["maxdd"], oos_liq=o["liq"],
                               oos_sl=o.get("sl_hits", 0))
                row["lev_max"] = max(r["cand"].get("lev", [0]))
            rows.append(row)
        folds[cid] = rows
    finals = {}
    for p in glob.glob("final_config_*.json"):
        finals[os.path.basename(p)[13:-5]] = json.load(open(p))
    stress = json.load(open("stress_shift.json")) if os.path.exists("stress_shift.json") else {}
    data = dict(resim=resim, folds=folds, finals=finals, stress=stress,
                baselines=baselines(),
                meta=dict(generated=str(pd.Timestamp.now()),
                          oos_span="2024-11-15 to 2026-07-01 (19.3 months)",
                          data_span="2023-11-27 to 2026-07-01 (CoinAPI, MEXC SOL_USDT perp, 3-min)",
                          data_gaps=["2024-06-02 to 2024-11-14 missing (CoinAPI)",
                                     "2025-11-10 four days missing"],
                          refit="parameters re-optimized every 28 days; expanding or rolling train window"))
    json.dump(data, open(OUT, "w"), default=float)
    print("wrote", OUT, os.path.getsize(OUT) // 1024, "KB")

if __name__ == "__main__":
    main()
