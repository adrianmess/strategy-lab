#!/usr/bin/env python3
"""Aggregate wf2 fold results into per-config OOS summaries + dashboard JSON."""
import json, os, glob
import numpy as np
import pandas as pd

def load_config(cid, base="wf2_results"):
    folds = []
    for p in sorted(glob.glob(os.path.join(base, cid, "fold_*.json"))):
        folds.append(json.load(open(p)))
    return folds

def summarize(cid, base="wf2_results"):
    folds = load_config(cid, base)
    rows = []
    tot_lg, mos = 0.0, 0.0
    liq = 0; slh = 0; worst_dd = 0.0; worst_mae = 0.0; n_tr = 0
    n_nofeas = 0
    for f in folds:
        if f["status"] != "ok" or not f.get("oos"):
            n_nofeas += 1
            continue
        o = f["oos"]
        tot_lg += o["growth"] * o["months"]
        mos += o["months"]
        liq += int(o["liq"]); slh += o.get("sl_hits", 0)
        worst_dd = max(worst_dd, o["maxdd"])
        worst_mae = min(worst_mae, o["worst_mae"])
        n_tr += o["n"]
        rows.append(dict(fold=f["job"]["fold_idx"], growth=o["growth"],
                         months=o["months"], n=o["n"], maxdd=o["maxdd"],
                         liq=o["liq"], sl_hits=o.get("sl_hits", 0)))
    if mos == 0:
        return dict(cid=cid, status="empty", n_nofeasible=n_nofeas)
    return dict(cid=cid, months=mos, total_mult=float(np.exp(tot_lg)),
                monthly_growth_pct=float((np.exp(tot_lg / mos) - 1) * 100),
                oos_liq_events=liq, oos_sl_hits=slh,
                worst_fold_dd=worst_dd, worst_mae=worst_mae,
                trades=n_tr, tpm=n_tr / mos,
                pos_folds=int(sum(1 for r in rows if r["growth"] > 0)),
                n_folds=len(rows), n_nofeasible=n_nofeas, folds=rows)

def main():
    cids = sorted(os.listdir("wf2_results"))
    out = [summarize(c) for c in cids]
    json.dump(out, open("wf2_summary.json", "w"), indent=1, default=float)
    rows = [o for o in out if o.get("months")]
    df = pd.DataFrame([{k: v for k, v in o.items() if k != "folds"} for o in rows])
    df = df.sort_values("monthly_growth_pct", ascending=False)
    pd.set_option("display.width", 250)
    print(df.to_string(index=False))

if __name__ == "__main__":
    main()
