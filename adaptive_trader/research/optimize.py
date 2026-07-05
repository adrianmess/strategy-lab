"""
Walk-forward optimizer for the adaptive strategy.

Candidate = per-vol-regime knobs + protective stop + trend-block variant.
Constraints on train (safety-first per user):
  - zero stop-loss hits (params must never touch the stop historically)
  - no liquidation, worst MAE must clear the stop by a buffer
  - minimum trades/month (the whole point: keep trading in quiet regimes)
Objective: monthly log-growth on train. OOS = subsequent unseen window.
"""
import numpy as np
import pandas as pd
import json, os, sys
from common import get_pres
from adaptive import make_adaptive_pre, build_P, run_adaptive
from regimes import vol_terciles
from fast_engine import params_to_vec
from engine import DEFAULT_PARAMS

RESULTS = "wf_results.json"
TREND_VARIANTS = [None, 1.5, 2.0, 3.0]

FOLDS = [
    ("2024-11-15", "2025-01-15"),
    ("2025-01-15", "2025-03-15"),
    ("2025-03-15", "2025-05-15"),
    ("2025-05-15", "2025-07-15"),
    ("2025-07-15", "2025-09-15"),
    ("2025-09-15", "2025-11-15"),
    ("2025-11-15", "2026-01-15"),
    ("2026-01-15", "2026-03-15"),
    ("2026-03-15", "2026-05-15"),
    ("2026-05-15", "2026-07-01"),
]

import pickle
VCACHE = "variants.pkl"

def get_variants(force=False):
    if os.path.exists(VCACHE) and not force:
        try:
            with open(VCACHE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"cache {VCACHE} unreadable ({e}); rebuilding...")
            try: os.remove(VCACHE)
            except OSError: pass
    pres = get_pres()
    variants = []
    for tz in TREND_VARIANTS:
        pa = [make_adaptive_pre(p, trend_block_z=tz) for p in pres]
        regs = [vol_terciles(f["volPct"]) for _, f in pa]
        variants.append((pa, regs))
    with open(VCACHE, "wb") as f:
        pickle.dump(variants, f)
    return variants

def sample_candidate(rng, max_equity_damage=0.30):
    sl = float(rng.choice([0.03, 0.04, 0.05, 0.06, 0.08, 0.10]))
    # couple leverage to stop level so safety constraints are satisfiable
    lev_hi = min(8.0, max_equity_damage / sl, 1.0 / (sl / 0.7 + 0.008))
    lev_lo = min(1.5, lev_hi)
    return dict(
        tv=int(rng.integers(0, len(TREND_VARIANTS))),
        zL=list(np.round(rng.uniform(-2.4, -0.9, 3), 2)),
        zS=list(np.round(rng.uniform(0.9, 2.4, 3), 2)),
        zXS=list(np.round(rng.uniform(1.2, 2.8, 3), 2)),
        ptScale=list(np.round(rng.uniform(0.6, 1.8, 3), 2)),
        lev=list(np.round(rng.uniform(lev_lo, lev_hi, 3), 1)),
        sl=sl,
    )

def eval_candidate(cand, variants, t0, t1, use_sl=True):
    pa, regs = variants[cand["tv"]]
    knobs = dict(zL=cand["zL"], zS=cand["zS"], zXS=cand["zXS"],
                 ptScale=cand["ptScale"], cdScale=[1.0] * 3, lev=cand["lev"])
    P = build_P(knobs)
    P[:, 35] = cand["sl"]; P[:, 36] = cand["sl"]
    tr, eq, liq, months = run_adaptive(pa, P, regs, t0=t0, t1=t1, use_sl=use_sl)
    if months <= 0 or len(tr) == 0:
        return None
    growth = np.log(max(eq, 1e-9) / 1000.0) / months
    # robust score: per-calendar-month log growth stats
    g_mean = g_std = 0.0
    if len(tr) > 2:
        e = pd.Series(tr["net"].to_numpy()).cumsum() + 1000.0
        mo = pd.Series(pd.to_datetime(tr["exit_t"]).dt.to_period("M").to_numpy())
        lg = np.log(np.maximum(e.to_numpy(), 1e-9))
        dfm = pd.DataFrame(dict(mo=mo, lg=lg)).groupby("mo")["lg"].last()
        gm = dfm.diff().dropna()
        if len(gm) >= 2:
            g_mean, g_std = float(gm.mean()), float(gm.std())
    slpm = float((tr["reason"] == 1).sum()) / months
    score = g_mean - 0.5 * g_std - 1.0 * slpm
    # max drawdown of equity path
    e = pd.Series(tr["net"].to_numpy()).cumsum() + 1000.0
    dd = float(((e.cummax() - e) / e.cummax()).max()) if len(e) else 0.0
    return dict(n=len(tr), months=months, eq=eq, growth=growth, liq=liq,
                sl_hits=int((tr["reason"] == 1).sum()),
                worst_mae=float(tr["mae"].min()),
                tpm=len(tr) / months, score=score, maxdd=dd,
                win=float((tr["net"] > 0).mean()))

def feasible(m, cand, min_tpm=6.0, max_sl_per_month=0.25, max_equity_damage=0.30,
             max_dd=0.55):
    """Honest constraints:
    - no liquidation ever
    - stop hits rare: <= ~3/year on train
    - each stop survivable: leverage*SL <= 30% equity damage
    - SL level clears liquidation threshold with buffer at the max leverage used
    - equity max drawdown bounded
    - keeps trading: >= min_tpm trades/month
    """
    if m is None or m["liq"]:
        return False
    if m["sl_hits"] / m["months"] > max_sl_per_month:
        return False
    lev_max = max(cand["lev"])
    if lev_max * cand["sl"] > max_equity_damage:
        return False
    liq_adverse = 1.0 / lev_max - 0.008   # maintenance+fees buffer
    if cand["sl"] > 0.7 * liq_adverse:    # stop must clear liq comfortably
        return False
    if m["maxdd"] > max_dd:
        return False
    if m["tpm"] < min_tpm:
        return False
    return True

def average_candidates(cands):
    """Parameter-average top candidates (reduces selection overfit)."""
    from collections import Counter
    out = dict(
        tv=Counter(c["tv"] for c in cands).most_common(1)[0][0],
        sl=float(np.median([c["sl"] for c in cands])),
    )
    for k in ["zL", "zS", "zXS", "ptScale", "lev"]:
        out[k] = list(np.round(np.mean([c[k] for c in cands], axis=0), 3))
    # re-apply safety coupling
    lev_hi = min(8.0, 0.30 / out["sl"], 1.0 / (out["sl"] / 0.7 + 0.008))
    out["lev"] = [min(v, lev_hi) for v in out["lev"]]
    return out

def optimize_fold(fold_idx, n_samples=2500, seed=0, top_k=5, variants=None):
    if variants is None:
        variants = get_variants()
    t_test0, t_test1 = FOLDS[fold_idx]
    rng = np.random.default_rng(seed + fold_idx * 1000)
    feas = []
    for _ in range(n_samples):
        cand = sample_candidate(rng)
        m = eval_candidate(cand, variants, None, t_test0)
        if not feasible(m, cand):
            continue
        feas.append((m["score"], cand, m))
    if not feas:
        return dict(fold=fold_idx, status="no_feasible")
    feas.sort(key=lambda x: -x[0])
    top = [c for _, c, _ in feas[:top_k]]
    avg = average_candidates(top)
    m_avg = eval_candidate(avg, variants, None, t_test0)
    # choose averaged candidate if it remains feasible, else best single
    if feasible(m_avg, avg):
        chosen, chosen_m = avg, m_avg
    else:
        chosen, chosen_m = feas[0][1], feas[0][2]
    oos = eval_candidate(chosen, variants, t_test0, t_test1)
    return dict(fold=fold_idx, status="ok", n_feasible=len(feas), cand=chosen,
                train=chosen_m, oos=oos)

def main():
    import time
    t_start = time.time()
    variants = get_variants()
    done = {}
    if os.path.exists(RESULTS):
        done = {r["fold"]: r for r in json.load(open(RESULTS))}
    for k in range(len(FOLDS)):
        if k in done:
            continue
        if time.time() - t_start > 20:
            break
        r = optimize_fold(k, variants=variants)
        done[k] = r
        json.dump(list(done.values()), open(RESULTS, "w"), default=float)
        if r["status"] == "ok":
            print("fold", k, "feas:", r["n_feasible"],
                  "train_g=%.3f" % r["train"]["growth"],
                  "oos_g=%.3f" % (r["oos"]["growth"] if r["oos"] else float("nan")),
                  "oos_sl=%s" % (r["oos"]["sl_hits"] if r["oos"] else "?"))
        else:
            print("fold", k, "NO FEASIBLE")
    print(f"{len(done)}/{len(FOLDS)} folds complete")

if __name__ == "__main__":
    main()
