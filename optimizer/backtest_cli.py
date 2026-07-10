#!/usr/bin/env python3
"""Run a full backtest of a config and publish it to the dashboard Backtests page.

Two input kinds:
  --config <best_config.json>   single parameter set -> continuous full-history run
  --walkforward <run_dir>       walk-forward run dir -> uses its continuous resim

Examples:
  python3 backtest_cli.py --config runs/my_lev_run/best_config.json --name my_lev_run
  python3 backtest_cli.py --walkforward runs/wf_lev --name wf_lev
  python3 backtest_cli.py --config ../adaptive_trader/research2/final_config_v6_lev_none.json --name production_lev
"""
import _bootstrap as B
import argparse, json, os, time
import numpy as np
import pandas as pd

BACKTESTS_JS = os.path.join(B.DASHBOARD, "backtests.js")
GAP_THRESHOLD_MIN = 1000   # data is split into continuous segments at gaps
                           # larger than this; nothing is ever traded across
                           # a gap and no P&L accrues inside one.


def gap_info():
    """Describe the gaps that segmentation skips (from the actual data)."""
    from common import load_segments
    segs = load_segments()
    spans = [(str(g["t"].min())[:16], str(g["t"].max())[:16]) for g, _ in segs]
    skipped = []
    for (a, b), (c, d) in zip(spans[:-1], spans[1:]):
        skipped.append(f"{b} -> {c}")
    return dict(mode="skip_segments", threshold_min=GAP_THRESHOLD_MIN,
                n_segments=len(segs), segments=spans, skipped_gaps=skipped)


def contamination_mask(t, warmup):
    from wf2 import contamination_mask as _cm
    return _cm(t, warmup)


ORIGINAL_STRATEGIES = ("v5_original", "prime_original")

def original_defaults(strategy, mode):
    """Every pine input of the ORIGINAL script, unmodified logic, single set."""
    from engine import DEFAULT_PARAMS
    p = {k: (1.0 if v is True else 0.0 if v is False else v)
         for k, v in DEFAULT_PARAMS.items()
         if k not in ("cdTfLong", "cdTfShort", "xCdTfLong", "xCdTfShort",
                      "initial_capital", "commission")}
    if strategy == "prime_original":
        from wf2 import PRIME_BASE
        for k, v in PRIME_BASE.items():
            if k in p and isinstance(v, (int, float)) and not isinstance(v, bool):
                p[k] = v
        p["enableLongX"] = 0.0
        p["enableShortX"] = 0.0
    else:
        p.setdefault("enableLongX", 1.0)
        p.setdefault("enableShortX", 1.0)
    p.setdefault("enableLong3m", 1.0)
    p.setdefault("enableShort3m", 1.0)
    if mode == "spot":
        p["leverage"] = 1.0
        p["enableShort3m"] = 0.0
        p["enableShortX"] = 0.0
    return p


def run_single_original(cfg, oos_start=None, holdout_days=None,
                        gap_mode="skip_contaminated"):
    """The ORIGINAL strategy engine: raw %-thresholds, ATR-switched EMAs, every
    indicator length recomputed fresh from the submitted parameters."""
    from engine import DEFAULT_PARAMS
    import fast_engine as fe
    from common import load_segments
    from wf2 import mtm_curve, contamination_mask, eval_intervals, FUT_COMM, SPOT_COMM
    from adaptive import slice_pre
    strategy, mode = cfg["strategy"], cfg.get("mode", "lev")
    p = dict(DEFAULT_PARAMS)
    p.update({k: v for k, v in cfg["cand"].items()})
    P = fe.params_to_vec(p, dict(
        enableLong3m=p.get("enableLong3m", 1.0), enableShort3m=p.get("enableShort3m", 1.0),
        enableLongX=p.get("enableLongX", 1.0), enableShortX=p.get("enableShortX", 1.0),
    )).reshape(1, -1)
    warmup = 3000
    eq = 1000.0
    trades, curve, open_positions = [], [], []
    mdd = 0.0; months = 0.0; liq_any = False
    suppressed = 0
    segs = load_segments()
    n_segs = len(segs)
    for si, (g, d1) in enumerate(segs):
        print(f"precomputing indicators, segment {si + 1}/{n_segs} ...", flush=True)
        pre = fe.precompute(g, d1, p)          # RAW indicators at the requested lengths
        t = pre["t"]
        seg_mask = (contamination_mask(t, warmup)
                    if gap_mode == "skip_contaminated" else None)
        if seg_mask is not None:
            suppressed += int(seg_mask.sum())
        i0 = warmup if oos_start is None else max(warmup, int(np.searchsorted(t, np.datetime64(oos_start))))
        i1 = len(t)
        if i1 - i0 < 200:
            continue
        ivs = [(i0, i1)] if not holdout_days else eval_intervals(t, i0, i1,
                dict(days=holdout_days, part="holdout"))
        regime = np.zeros(len(t), dtype=np.int32)
        for iv_i, (a, b) in enumerate(ivs):
            w0 = max(0, a - warmup)
            sp = slice_pre(pre, w0, b)
            eq0 = eq
            tr, eq, liq, op = fe.run_fast(sp, P, regime=regime[w0:b], warmup=a - w0,
                                          initial_capital=eq, use_sl=True,
                                          commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                          liq_threshold=-1.0 if mode == "lev" else 1e9,
                                          return_open=True,
                                          no_entry=(seg_mask[w0:b] if seg_mask is not None else None))
            if op:
                is_end = (si == n_segs - 1 and iv_i == len(ivs) - 1)
                if is_end or not holdout_days:
                    mark = float(sp["c"][-1])
                    open_positions.append(dict(
                        dir=("long" if op["dir"] > 0 else "short"),
                        entry_t=op["entry_t"][:16], entry=op["entry"],
                        lev=op["lev"], mark=mark, as_of=str(sp["t"][-1])[:16],
                        unreal=float(op["qty"] * (mark - op["entry"]) * op["dir"]),
                        move_pct=float((mark / op["entry"] - 1) * op["dir"]),
                        at=("end of data" if is_end else "data-gap boundary (dropped, not counted)")))
            months += (b - a) / (480 * 30.4)
            if len(tr):
                m, d = mtm_curve(tr, sp["c"], initial=eq0)
                mdd = max(mdd, d)
                step = max(1, len(m) // 500)
                if curve and curve[-1]["eq"] is not None:
                    curve.append(dict(t="(data gap)", eq=None))
                for x, v in zip(pd.to_datetime(sp["t"][::step]), m[::step]):
                    if np.isfinite(v):
                        curve.append(dict(t=str(x), eq=float(v)))
                trades.append(tr)
            if liq:
                liq_any = True
                break
        if liq_any:
            break
    tr = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    return build_entry(tr, eq, months, mdd, liq_any, curve, open_positions=open_positions,
                       label_extra=dict(gap_mode=gap_mode, suppressed_bars=suppressed,
                                        strategy=strategy, mode=mode, method="none",
                                        kind=("original engine (full pine logic)"
                                              if oos_start is None else f"original, from {oos_start}"),
                                        config=cfg["cand"]))


def opt_settings(cfg):
    """Optimizer settings recorded in a best_config, for display on dashboards."""
    if not isinstance(cfg, dict) or "algo" not in cfg and "evaluated" not in cfg:
        return None
    ho = (f"alternating {cfg['holdout_days']:g}d blocks" if cfg.get("holdout_days")
          else f"after {cfg['train_end']}" if cfg.get("train_end") else "none")
    return dict(algo=cfg.get("algo"),
                param_set=("per-regime" if cfg.get("per_regime", True) else "single set"),
                holdout=ho, max_dd=cfg.get("max_dd"),
                max_hold_days=cfg.get("max_hold_days"),
                gap_mode=cfg.get("gap_mode"),
                lockbox=cfg.get("lockbox"),
                scoring=cfg.get("scoring"),
                anchor=cfg.get("anchor"), anchor_strength=cfg.get("anchor_strength"),
                evaluated=cfg.get("evaluated"))


def run_single_v7(cfg, oos_start=None, holdout_days=None, gap_mode="skip_contaminated"):
    """Backtest a V7 (engine3, full-param) candidate."""
    import optimizer2 as O
    from engine3 import run3
    from wf2 import mtm_curve
    from adaptive import slice_pre
    cand, method, mode = cfg["cand"], cfg["method"], cfg["cand"]["mode"]
    G = O.load_g3()
    regs_list, R = G["regimes"][method]
    P = O.build_P3(cand)
    if P.shape[0] != R:
        import numpy as _np
        P = _np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
    eq = 1000.0; trades, curve = [], []
    mdd = 0.0; months = 0.0; liq_any = False
    open_positions = []
    warmup = 3000
    n_segs = len(G["pres"])
    suppressed = 0
    for si, (pre, reg) in enumerate(zip(G["pres"], regs_list)):
        t = pre["t"]
        seg_mask = contamination_mask(t, 3000) if gap_mode == "skip_contaminated" else None
        if seg_mask is not None:
            suppressed += int(seg_mask.sum())
        i0 = warmup if oos_start is None else max(warmup, int(np.searchsorted(t, np.datetime64(oos_start))))
        i1 = len(t)
        if i1 - i0 < 200:
            continue
        from wf2 import alt_intervals
        ivs = [(i0, i1)] if not holdout_days else alt_intervals(t, i0, i1, holdout_days, "holdout")
        for iv_i, (a, b) in enumerate(ivs):
            w0 = max(0, a - warmup)
            sp = slice_pre(pre, w0, b)
            eq0 = eq
            tr, eq, liq, op = run3(sp, P, regime=reg[w0:b], warmup=a - w0,
                                   initial_capital=eq,
                                   commission=0.0004 if mode == "lev" else 0.0005,
                                   use_sl=(mode == "spot"), dyn_liq=(mode == "lev"),
                                   return_open=True,
                                   no_entry=(seg_mask[w0:b] if seg_mask is not None else None))
            if op:
                is_end = (si == n_segs - 1 and iv_i == len(ivs) - 1)
                if is_end or not holdout_days:
                    mark = float(sp["c"][-1])
                    open_positions.append(dict(
                        dir=("long" if op["dir"] > 0 else "short"),
                        entry_t=op["entry_t"][:16], entry=op["entry"],
                        lev=op["lev"], mark=mark, as_of=str(sp["t"][-1])[:16],
                        unreal=float(op["qty"] * (mark - op["entry"]) * op["dir"]),
                        move_pct=float((mark / op["entry"] - 1) * op["dir"]),
                        at=("end of data" if is_end else "data-gap boundary (dropped, not counted)")))
            months += (b - a) / (480 * 30.4)
            if len(tr):
                m, d = mtm_curve(tr, sp["c"], initial=eq0)
                mdd = max(mdd, d)
                step = max(1, len(m) // 500)
                if curve and curve[-1]["eq"] is not None:
                    curve.append(dict(t="(data gap)", eq=None))  # break the chart line
                for x, v in zip(pd.to_datetime(sp["t"][::step]), m[::step]):
                    if np.isfinite(v):
                        curve.append(dict(t=str(x), eq=float(v)))
                trades.append(tr)
            if liq:
                liq_any = True
                break
        if liq_any:
            break
    tr = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    return build_entry(tr, eq, months, mdd, liq_any, curve, open_positions=open_positions,
                       label_extra=dict(gap_mode=gap_mode, suppressed_bars=suppressed,
                                        strategy=cfg.get("strategy") or cand.get("strategy", "v7"),
                                        mode=mode, method=method,
                                        kind=(f"alternating holdout ({holdout_days:g}d blocks)" if holdout_days else "full-history (in-sample fit)" if oos_start is None else f"from {oos_start}"),
                                        config=cand, opt=opt_settings(cfg)))


def run_single(cfg_path, oos_start=None, holdout_days=None, gap_mode="skip_contaminated"):
    cfg = json.load(open(cfg_path))
    cand = cfg.get("cand")
    if not cand:
        raise SystemExit("This run produced NO surviving candidate (see its report) — "
                         "there is nothing to backtest.")
    if cfg.get("strategy") in ORIGINAL_STRATEGIES:
        return run_single_original(cfg, oos_start, holdout_days=holdout_days,
                                   gap_mode=gap_mode)
    if cfg.get("strategy") in ("v7", "prime7") or cand.get("strategy") in ("v7", "prime7") \
            or "regs" in cand:
        return run_single_v7(cfg, oos_start, holdout_days=holdout_days, gap_mode=gap_mode)
    strategy, mode, method = cfg["strategy"], cfg["mode"], cfg["method"]
    from wf2 import (load_globals, build_P_v6, build_P_scalpx, build_P_prime,
                     mtm_curve, FUT_COMM, SPOT_COMM)
    from fast_engine import run_fast
    from scalp_engine import run_scalp, run_scalp2, slice_pre2
    from adaptive import slice_pre
    from regimes import DAY
    G = load_globals(("v6",) if strategy == "prime" else (strategy,))
    R = G["nreg"][method]
    sx2 = strategy == "scalpx2"
    vidx = None
    if sx2:
        from wf2 import build_P_scalpx2
        P, vidx = build_P_scalpx2(cand, R)
    else:
        builder = {"v6": build_P_v6, "prime": build_P_prime}.get(strategy, build_P_scalpx)
        P = builder(cand, R)
    v6like = strategy in ("v6", "prime")
    segs = G["v6"][cand.get("tv", 0)] if v6like else (G["scalp2"] if sx2 else G["scalp"])
    regs = G["regimes_v6" if v6like else ("regimes_sc2" if sx2 else "regimes_sc")][method]
    warmup = 3000 if strategy == "v6" else 2500
    eq = 1000.0
    trades, curve = [], []
    mdd = 0.0; months = 0.0; liq_any = False
    open_positions = []
    n_segs = len(segs)
    suppressed = 0
    for si, ((pre, f), reg) in enumerate(zip(segs, regs)):
        t = pre["t"]
        seg_mask = contamination_mask(t, warmup) if gap_mode == "skip_contaminated" else None
        if seg_mask is not None:
            suppressed += int(seg_mask.sum())
        i0 = warmup if oos_start is None else max(warmup, int(np.searchsorted(t, np.datetime64(oos_start))))
        i1 = len(t)
        if i1 - i0 < 200:
            continue
        from wf2 import alt_intervals
        ivs = [(i0, i1)] if not holdout_days else alt_intervals(t, i0, i1, holdout_days, "holdout")
        for iv_i, (a, b) in enumerate(ivs):
            w0 = max(0, a - warmup)
            sp = slice_pre2(pre, w0, b) if sx2 else slice_pre(pre, w0, b)
            eq0 = eq
            if sx2:
                tr, eq, liq, op = run_scalp2(sp, P, vidx, regime=reg[w0:b], warmup=a - w0,
                                             initial_capital=eq,
                                             commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                             liq_threshold=-1.0 if mode == "lev" else 1e9,
                                             return_open=True,
                                             no_entry=(seg_mask[w0:b] if seg_mask is not None else None))
            elif v6like:
                tr, eq, liq, op = run_fast(sp, P, regime=reg[w0:b], warmup=a - w0,
                                           initial_capital=eq, use_sl=(mode == "spot"),
                                           commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                           liq_threshold=-1.0 if mode == "lev" else 1e9,
                                           return_open=True,
                                           no_entry=(seg_mask[w0:b] if seg_mask is not None else None))
            else:
                tr, eq, liq, op = run_scalp(sp, P, regime=reg[w0:b], warmup=a - w0,
                                            initial_capital=eq,
                                            commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                            liq_threshold=-1.0 if mode == "lev" else 1e9,
                                            return_open=True,
                                            no_entry=(seg_mask[w0:b] if seg_mask is not None else None))
            if op:
                is_end = (si == n_segs - 1 and iv_i == len(ivs) - 1)
                if is_end or not holdout_days:
                    mark = float(sp["c"][-1])
                    open_positions.append(dict(
                        dir=("long" if op["dir"] > 0 else "short"),
                        entry_t=op["entry_t"][:16], entry=op["entry"],
                        lev=op["lev"], mark=mark, as_of=str(sp["t"][-1])[:16],
                        unreal=float(op["qty"] * (mark - op["entry"]) * op["dir"]),
                        move_pct=float((mark / op["entry"] - 1) * op["dir"]),
                        at=("end of data" if is_end else "data-gap boundary (dropped, not counted)")))
            months += (b - a) / (480 * 30.4)
            if len(tr):
                m, d = mtm_curve(tr, sp["c"], initial=eq0)
                mdd = max(mdd, d)
                step = max(1, len(m) // 500)
                if curve and curve[-1]["eq"] is not None:
                    curve.append(dict(t="(data gap)", eq=None))  # break the chart line
                for x, v in zip(pd.to_datetime(sp["t"][::step]), m[::step]):
                    if np.isfinite(v):
                        curve.append(dict(t=str(x), eq=float(v)))
                trades.append(tr)
            if liq:
                liq_any = True
                break
        if liq_any:
            break
    tr = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    return build_entry(tr, eq, months, mdd, liq_any, curve, open_positions=open_positions,
                       label_extra=dict(gap_mode=gap_mode, suppressed_bars=suppressed,
                                        strategy=strategy, mode=mode, method=method,
                                        kind=(f"alternating holdout ({holdout_days:g}d blocks)" if holdout_days else "full-history (in-sample fit)" if oos_start is None else f"from {oos_start}"),
                                        config=cand, opt=opt_settings(cfg)))


def build_entry(tr, eq, months, mdd, liq, curve, label_extra, open_positions=None):
    g = np.log(max(eq, 1e-9) / 1000.0) / max(months, 1e-9)
    tl = []
    if len(tr):
        for _, r in tr.tail(2000).iterrows():
            tl.append(dict(entry_t=str(r.get("entry_t", "")), exit_t=str(r.get("exit_t", "")),
                           dir=("long" if r["dir"] > 0 else "short"),
                           entry=float(r["entry"]), exit=float(r["exit"]),
                           net=float(r["net"]), mae=float(r["mae"]),
                           reason={0: "profit_target", 1: "stop_loss", 2: "LIQUIDATED"}.get(int(r["reason"]), "?"),
                           lev=float(r.get("lev", 1.0))))
        mo = (pd.to_datetime(tr["exit_t"]).dt.to_period("M").astype(str)
              if "exit_t" in tr else None)
        e = tr["net"].cumsum() + 1000.0
        lg = np.log(np.maximum(e, 1e-9))
        monthly = (pd.DataFrame(dict(mo=mo, lg=lg)).groupby("mo")["lg"].last().diff())
        monthly.iloc[0] = np.log(np.maximum(e.iloc[0], 1e-9) / 1000) if len(monthly) else 0
        monthly_tbl = [dict(month=k, ret_pct=float((np.exp(v) - 1) * 100))
                       for k, v in monthly.items() if np.isfinite(v)]
    else:
        monthly_tbl = []
    return dict(stats=dict(months=months, final_eq=float(eq), total_mult=eq / 1000.0,
                           monthly_growth_pct=float((np.exp(g) - 1) * 100),
                           liq=bool(liq), maxdd_mtm=mdd, n=int(len(tr)),
                           tpm=len(tr) / max(months, 1e-9),
                           sl_hits=int((tr["reason"] == 1).sum()) if len(tr) else 0,
                           worst_mae=float(tr["mae"].min()) if len(tr) else 0.0,
                           win=float((tr["net"] > 0).mean()) if len(tr) else 0.0,
                           open_at_end=bool(open_positions and
                                            any(o["at"] == "end of data" for o in open_positions))),
                curve=curve, trades=tl, monthly=monthly_tbl,
                open_positions=(open_positions or []), **label_extra)


def load_backtests():
    if not os.path.exists(BACKTESTS_JS):
        return []
    txt = open(BACKTESTS_JS).read()
    return json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))


def save_backtests(entries):
    with open(BACKTESTS_JS, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="best_config.json / final_config_*.json")
    ap.add_argument("--walkforward", help="walk-forward run dir (uses resim.json)")
    ap.add_argument("--name", required=True)
    ap.add_argument("--holdout-days", type=float, default=None,
                    help="evaluate only the alternating held-out blocks of N days")
    ap.add_argument("--gap-mode", default="skip_contaminated",
                    choices=["skip_open", "skip_contaminated"],
                    help="skip_open: drop trades open at gap boundaries (always on). "
                         "skip_contaminated (default): also suppress entries while "
                         "indicators re-converge after small in-segment gaps.")
    ap.add_argument("--oos-start", default=None,
                    help="only simulate from this date (single-config mode)")
    args = ap.parse_args()

    cwd0 = os.getcwd()
    B.enter_run_dir("_backtest_tmp")
    if args.config:
        path = args.config if os.path.isabs(args.config) else os.path.join(cwd0, args.config)
        entry = run_single(path, args.oos_start, holdout_days=args.holdout_days,
                           gap_mode=args.gap_mode)
        # flag the outcome on the source optimizer run (runs table shows it)
        run_dir = os.path.dirname(os.path.abspath(path))
        if os.path.basename(os.path.dirname(run_dir)) == "runs":
            fp = os.path.join(run_dir, "backtest_flags.json")
            try:
                flags = json.load(open(fp)) if os.path.exists(fp) else {}
            except Exception:
                flags = {}
            key = f"{os.path.basename(path)}|{entry.get('kind', 'full')}"
            flags[key] = dict(source=os.path.basename(path), kind=entry.get("kind"),
                              liq=bool(entry["stats"]["liq"]),
                              growth_pct=float(entry["stats"]["monthly_growth_pct"]),
                              gap_mode=args.gap_mode, backtest=args.name,
                              at=time.strftime("%Y-%m-%d %H:%M"))
            json.dump(flags, open(fp, "w"), indent=1)
    else:
        wf_dir = args.walkforward if os.path.isabs(args.walkforward) else os.path.join(cwd0, args.walkforward)
        r = json.load(open(os.path.join(wf_dir, "resim.json")))
        entry = dict(stats={k: v for k, v in r.items() if k not in ("curve", "cid", "status")},
                     curve=r.get("curve", []), trades=[], monthly=[],
                     strategy=r["cid"].split("__")[0], mode=r["cid"].split("__")[1],
                     method=r["cid"].split("__")[2], kind="walk-forward continuous OOS",
                     config=None)
    entry["name"] = args.name
    entry["created"] = str(pd.Timestamp.now())[:16]
    entry["gap_handling"] = gap_info()
    entries = [e for e in load_backtests() if e["name"] != args.name]
    entries.append(entry)
    save_backtests(entries)
    print(f"published '{args.name}' -> dashboard/backtests.html ({len(entries)} entries)")
    print(json.dumps(entry["stats"], indent=1, default=float))


if __name__ == "__main__":
    main()
