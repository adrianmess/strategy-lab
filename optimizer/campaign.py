#!/usr/bin/env python3
"""Autonomous experiment campaign runner.

Runs a curated matrix of optimizer experiments SEQUENTIALLY (each using all
--procs workers), then iterates on the results automatically:

  wave 1: broad matrix — strategies x modes x regimes x scorings x spaces,
          all with max-hold <= 5 days and a bias toward many-small-gains
          (tight profit-target "scalp" space variants)
  wave 2: exploitation — refine the top wave-1 survivors; breed-merge groups
          of compatible positives
  wave 3: full-history backtests of every surviving OOS-best config
  report: campaigns/<name>/report.md ranked by honest (holdout) numbers,
          with trades/month and avg gain per trade (small-gains preference)

Stop/resume:
  touch campaigns/<name>/STOP     -> current run finishes its generation,
                                     finalizes (holdout eval), campaign exits
  rerun campaign.py --name <name> -> resumes where it left off (optimize2
                                     itself resumes same-named runs)

All run names start with "camp_<name>_" so they're identifiable and
filterable on the Optimize page.

Usage:
  cd optimizer && python3 campaign.py --name c1 --procs 14
"""
import argparse, json, math, os, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
CAMPS = os.path.join(HERE, "campaigns")
TRAIN_END = "2025-09-01"


# ---------------- space variants (small-gains bias) ----------------
_PT_KEYS = ("ptLong", "ptShort", "pt", "tpL", "tpS", "xTpLong", "xTpShort")
_DUR_KEYS_PREFIX = ("dur1", "dur2", "xDur1", "xDur2", "d1L", "d1S", "d2L", "d2S")
_SL_KEYS = ("slLong", "slShort", "slL", "slS", "sl")

def make_space_variant(base_path, out_path, tight=False, rocx_tslm=None,
                       lev_cap=None, sl_cap=None):
    """Copy param_space.json; 'tight' clamps profit targets to 0.2%-2% and
    trade durations to <= 2 days (960 bars) -> many small gains by construction.
    lev_cap caps searchable leverage; sl_cap tightens stop-losses (with lev<=4
    and sl<=4% a stop fires ~6x before the liquidation line -> liq-proof).
    rocx_tslm: None=leave, [0,1]=search both stop modes, [1]=force ratchet."""
    sp = json.load(open(base_path))
    for skey, entry in sp.items():
        if not isinstance(entry, dict):
            continue
        cont = entry.get("continuous") or {}
        for k, spec in cont.items():
            lo, hi = spec.get("range", [None, None])
            if lo is None:
                continue
            if tight:
                if k in _PT_KEYS:
                    spec["range"] = [max(lo, 0.002), min(hi, 0.02) if hi > 0.002 else hi]
                elif any(k.startswith(p) for p in _DUR_KEYS_PREFIX):
                    spec["range"] = [lo, min(hi, 960)]   # <= 2 days of 3-min bars
                elif k == "tsl" and sl_cap is None:
                    spec["range"] = [max(lo, 0.004), min(hi, 0.03)]
            lo, hi = spec["range"]
            if lev_cap is not None and k == "leverage":
                spec["range"] = [min(lo, lev_cap), min(hi, lev_cap)]
            if sl_cap is not None and (k in _SL_KEYS or k == "tsl"):
                spec["range"] = [max(min(lo, sl_cap * 0.2), 0.004), min(hi, sl_cap)]
        if rocx_tslm is not None and skey.startswith("rocx"):
            m = (entry.get("menus") or {}).get("tslm")
            if m is not None:
                m["options"] = list(rocx_tslm)
    json.dump(sp, open(out_path, "w"), indent=1)
    return out_path


# ---------------- wave-1 matrix ----------------
def wave1_specs():
    """Curated from analysis of all 156 prior runs (see report header).
    d=default space, t=tight space; evals scaled by engine cost."""
    S = []
    def add(sid, strat, mode, method, scoring, space, total, mh=5, dd=None, extra=None):
        S.append(dict(id=sid, wave=1, strategy=strat, mode=mode, method=method,
                      scoring=scoring, space=space, total=total, mh=mh, dd=dd,
                      extra=extra or [], status="pending"))
    # --- v7 lev: the proven family, now under mh5 + tight variants
    add("w1_01_v7_vol3_uw_d",   "v7", "lev", "vol3", "underwater", "default", 100000)
    add("w1_02_v7_vol3_cl_d",   "v7", "lev", "vol3", "classic",    "default", 100000)
    add("w1_03_v7_vol3_uw_t",   "v7", "lev", "vol3", "underwater", "tight",   100000)
    add("w1_04_v7_vol3_cl_t",   "v7", "lev", "vol3", "classic",    "tight",   100000)
    add("w1_05_v7_trend3_uw_d", "v7", "lev", "trend3", "underwater", "default", 100000)
    add("w1_06_v7_trend3_cl_t", "v7", "lev", "trend3", "classic",  "tight",   100000)
    add("w1_07_v7_vol37d_cl_d", "v7", "lev", "vol3_7d", "classic", "default", 100000)
    add("w1_08_v7_vol37d_uw_t", "v7", "lev", "vol3_7d", "underwater", "tight", 100000)
    add("w1_09_v7_volume3_cl_d","v7", "lev", "volume3", "classic", "default", 100000)
    add("w1_10_v7_vXt9_uw_d",   "v7", "lev", "volXtrend9", "underwater", "default", 100000)
    add("w1_11_v7_vol3_ww_d",   "v7", "lev", "vol3", "worst_window", "default", 100000)
    add("w1_12_v7_vol3_ww_t",   "v7", "lev", "vol3", "worst_window", "tight", 100000)
    # --- day-trade / intraday holds
    add("w1_13_v7_vol3_uw_mh1", "v7", "lev", "vol3", "underwater", "default", 100000, mh=1)
    add("w1_14_v7_vol3_cl_t_mh1","v7","lev", "vol3", "classic",    "tight",   100000, mh=1)
    # --- tighter drawdown appetite
    add("w1_15_v7_vol3_uw_dd35","v7", "lev", "vol3", "underwater", "default", 100000, dd=0.35)
    add("w1_16_v7_vol3_cl_dd35","v7", "lev", "vol3", "classic",    "default", 100000, dd=0.35)
    # --- prime7 / prime on lev (barely explored, 1/1 positive)
    add("w1_17_prime7_vol3_cl", "prime7", "lev", "vol3", "classic", "default", 100000)
    add("w1_18_prime7_trend3_cl","prime7","lev", "trend3", "classic", "default", 100000)
    add("w1_19_prime_vol3_cl_t","prime", "lev", "vol3", "classic",  "tight",   150000)
    add("w1_20_prime_trend3_cl","prime", "lev", "trend3", "classic","default", 150000)
    # --- scalpx2 lev (4/5 positive on underwater, never tried trend3/tight)
    add("w1_21_sx2_vol3_uw_d",  "scalpx2", "lev", "vol3", "underwater", "default", 150000)
    add("w1_22_sx2_vol3_cl_d",  "scalpx2", "lev", "vol3", "classic",    "default", 150000)
    add("w1_23_sx2_trend3_uw_d","scalpx2", "lev", "trend3", "underwater","default", 150000)
    add("w1_24_sx2_vol3_uw_t",  "scalpx2", "lev", "vol3", "underwater", "tight",   150000)
    add("w1_25_sx2_vol3_uw_mh05","scalpx2","lev", "vol3", "underwater", "default", 150000, mh=0.5)
    add("w1_26_sx1_vol3_cl_d",  "scalpx",  "lev", "vol3", "classic",    "default", 150000)
    # --- macdx on LEV: never tried (its scalpy 300-trades/6wk profile fits mh5)
    add("w1_27_macdx_vol3_cl_d","macdx", "lev", "vol3", "classic",    "default", 150000)
    add("w1_28_macdx_vol3_uw_t","macdx", "lev", "vol3", "underwater", "tight",   150000)
    add("w1_29_macdx_trend3_cl","macdx", "lev", "trend3", "classic",  "default", 150000)
    add("w1_30_macdx_vol3_cl_mh1","macdx","lev","vol3", "classic",    "tight",   150000, mh=1)
    # --- rocx on LEV as a bull-regime specialist + the NEW ratchet stop
    add("w1_31_rocx_vol3_cl_r", "rocx", "lev", "vol3", "classic", "ratchet_both", 150000)
    add("w1_32_rocx_trend3_cl_r","rocx","lev", "trend3", "classic", "ratchet_both", 150000)
    # --- v6 lev sanity point
    add("w1_33_v6_vol3_cl_d",   "v6", "lev", "vol3", "classic", "default", 100000)
    # --- spot side (harder; ratchet may fix rocx's slow-bleed problem)
    add("w1_34_prime_sp_tr3_t", "prime", "spot", "trend3", "classic", "tight", 100000)
    add("w1_35_prime_sp_tr3_uw","prime", "spot", "trend3", "underwater", "default", 100000)
    add("w1_36_macdx_sp_vol3_t","macdx", "spot", "vol3", "classic", "tight", 150000)
    add("w1_37_sx2_sp_tr3_uw_t","scalpx2","spot","trend3", "underwater", "tight", 150000)
    add("w1_38_rocx_sp_ratchet","rocx", "spot", "vol3", "classic", "ratchet_only", 150000)
    add("w1_39_v7_sp_vol3_cl_t","v7", "spot", "vol3", "classic", "tight", 100000)
    add("w1_40_sx1_sp_vol3_cl", "scalpx", "spot", "vol3", "classic", "default", 150000)
    return S


# ---------------- era-robustness matrix (campaign c2) ----------------
def era_specs():
    """Attack the mid-2025 cliff. Diagnosis: (1) train-end 2025-09-01 means all
    the sparkle before Sept-2025 is in-sample fit; (2) Sept-2025 starts a real
    bear + 2026 volatility compression. Cures tested here:
      alt21/alt42  : alternating-block holdouts -> training spans BOTH eras
      recent       : recency-weighted scoring (newest era dominates the score)
      ts2501/ts2509: era-specialist training windows (recent-only / bear-only)
      crossfit+lockbox: folds + untouched 2026-04.. lockbox as the final judge
    Families = the ones with real OOS life in campaign c1."""
    S = []
    def add(sid, strat, mode, method, space="default", scoring="classic",
            total=100000, mh=5, dd=None, hold=None, train_end="unset",
            train_start=None, algo="genetic", extra=None):
        S.append(dict(id=sid, wave=1, strategy=strat, mode=mode, method=method,
                      scoring=scoring, space=space, total=total, mh=mh, dd=dd,
                      hold=hold, train_start=train_start, algo=algo,
                      train_end=(TRAIN_END if train_end == "unset" else train_end),
                      extra=extra or [], status="pending"))
    fams = [("macdx", "vol3", "default"), ("macdx", "trend3", "default"),
            ("v7", "vol3", "default"), ("scalpx2", "vol3", "default"),
            ("rocx", "trend3", "ratchet_both"), ("prime", "trend3", "default")]
    for strat, meth, space in fams:
        tag = f"{strat[:5]}_{meth}"
        # training interleaves bull AND bear/chop blocks
        add(f"e_{tag}_alt21", strat, "lev", meth, space, "classic",
            hold=21, train_end=None)
        # same, but the newest months dominate the score
        add(f"e_{tag}_alt21rec", strat, "lev", meth, space, "recent",
            hold=21, train_end=None)
        # era specialist: train only on 2025+ (bull top, crash, chop),
        # holdout = the 2026 compression via alternating inside the window
        add(f"e_{tag}_ts2501", strat, "lev", meth, space, "classic",
            train_start="2025-01-01", train_end="2026-03-01")
    # bear/chop-only specialists (long+short strategies only — no long-only rocx)
    for strat, meth, space in [("macdx", "vol3", "default"), ("v7", "vol3", "default"),
                               ("scalpx2", "vol3", "default")]:
        add(f"e_{strat[:5]}_{meth}_ts2509", strat, "lev", meth, space, "classic",
            train_start="2025-09-01", train_end="2026-04-01")
    # coarser alternating blocks for the two strongest families
    add("e_macdx_vol3_alt42", "macdx", "lev", "vol3", "default", "classic",
        hold=42, train_end=None)
    add("e_v7_vol3_alt42", "v7", "lev", "vol3", "default", "classic",
        hold=42, train_end=None)
    # cross-fit with an untouched 2026-04.. lockbox as the final judge
    add("e_macdx_vol3_xfit", "macdx", "lev", "vol3", "default", "classic",
        algo="crossfit", hold=21, train_end=None,
        extra=["--lockbox", "2026-04-01.."])
    add("e_v7_vol3_xfit", "v7", "lev", "vol3", "default", "classic",
        algo="crossfit", hold=21, train_end=None,
        extra=["--lockbox", "2026-04-01.."])
    return S

# ---------------- survival matrix (campaign c3) ----------------
def survival_specs():
    """c2 verdict: cross-era gates found real edges (v7 bear-specialist +58%/mo,
    macdx crossfit +43%/mo) but HALF the matrix died at 85-100% DD -> the enemy
    is liquidation, not signal. This matrix makes liq structurally
    near-impossible instead of hoping the search avoids it:
      levsafe space : leverage <= 4 AND stops <= 4% -> a stop fires ~6x before
                      the liq line; tight PTs keep the many-small-gains profile
      --lev-stops   : v7/prime/v6 keep their stop-losses ACTIVE in lev mode
                      (classic lev has none by design — this is the opt-in)
      dd 0.40 gate  : drawdown appetite enforced at the search gate
    Windows reuse what WON in c2 (ts2509, ts2501, xfit, alt21)."""
    S = []
    def add(sid, strat, meth, space, scoring="classic", total=100000, dd=0.40,
            hold=None, train_end=None, train_start=None, algo="genetic",
            stops=False, extra=None):
        ex = list(extra or [])
        if stops:
            ex += ["--lev-stops"]
        S.append(dict(id=sid, wave=1, strategy=strat, mode="lev", method=meth,
                      scoring=scoring, space=space, total=total, mh=5, dd=dd,
                      hold=hold, train_start=train_start, train_end=train_end,
                      algo=algo, extra=ex, status="pending"))
    # --- macdx (native stops; just cap lev + tighten sl)
    add("s_macdx_vol3_xfit",  "macdx", "vol3", "levsafe", algo="crossfit",
        hold=21, extra=["--lockbox", "2026-04-01.."])
    add("s_macdx_vol3_alt21", "macdx", "vol3", "levsafe", hold=21)
    add("s_macdx_vol3_ts2501","macdx", "vol3", "levsafe",
        train_start="2025-01-01", train_end="2026-03-01")
    add("s_macdx_trend3_alt21","macdx","trend3","levsafe", hold=21)
    # --- v7 with ACTIVE stops (first time ever)
    add("s_v7_vol3_ts2509_sl", "v7", "vol3", "levsafe", stops=True,
        train_start="2025-09-01", train_end="2026-04-01")
    add("s_v7_vol3_ts2501_sl", "v7", "vol3", "levsafe", stops=True,
        train_start="2025-01-01", train_end="2026-03-01")
    add("s_v7_vol3_xfit_sl",   "v7", "vol3", "levsafe", stops=True,
        algo="crossfit", hold=21, extra=["--lockbox", "2026-04-01.."])
    add("s_v7_vol3_alt21_sl",  "v7", "vol3", "levsafe", stops=True, hold=21)
    add("s_v7_vol3_alt21rec_sl","v7","vol3", "levsafe", stops=True, hold=21,
        scoring="recent")
    # --- scalpx2 (ts wins in c2; now capped)
    add("s_sx2_vol3_ts2509", "scalpx2", "vol3", "levsafe",
        train_start="2025-09-01", train_end="2026-04-01", total=150000)
    add("s_sx2_vol3_ts2501", "scalpx2", "vol3", "levsafe",
        train_start="2025-01-01", train_end="2026-03-01", total=150000)
    add("s_sx2_vol3_alt21",  "scalpx2", "vol3", "levsafe", hold=21, total=150000)
    # --- prime with stops
    add("s_prime_trend3_alt21_sl", "prime", "trend3", "levsafe", stops=True,
        hold=21, total=150000)
    add("s_prime_trend3_ts2501_sl","prime", "trend3", "levsafe", stops=True,
        train_start="2025-01-01", train_end="2026-03-01", total=150000)
    # --- rocx: ratchet trail IS a tight stop; force it + cap lev
    add("s_rocx_trend3_alt21_r", "rocx", "trend3", "levsafe_ratchet", hold=21,
        total=150000)
    add("s_rocx_trend3_ts2501_r","rocx", "trend3", "levsafe_ratchet",
        train_start="2025-01-01", train_end="2026-03-01", total=150000)
    # --- v6 with stops (sanity)
    add("s_v6_vol3_alt21_sl", "v6", "vol3", "levsafe", stops=True, hold=21)
    # --- medium-lev comparison for the two strongest (lev<=6, sl<=6%)
    add("s_macdx_vol3_xfit_lev6", "macdx", "vol3", "levsafe6", algo="crossfit",
        hold=21, extra=["--lockbox", "2026-04-01.."])
    add("s_v7_vol3_ts2509_sl_lev6", "v7", "vol3", "levsafe6", stops=True,
        train_start="2025-09-01", train_end="2026-04-01")
    return S

# ---------------- meta-router matrix (campaign c4) ----------------
def meta_specs():
    """MetaX: one wrapped strategy that routes between the proven cross-era
    champions by market segment. Components frozen (wave 1 searches only the
    assignment); wave 2 joint-refines component params under the winning
    assignment. month12 = flagged seasonal-memorization research."""
    S = []
    for mode in ("lev", "spot"):
        for buckets in ("vt9", "vol3", "trend3", "month12"):
            S.append(dict(id=f"m_{mode}_{buckets}", wave=1, kind="metax",
                          strategy="metax", mode=mode, method=buckets,
                          scoring="classic", space="default", total=30000,
                          mh=7, status="pending"))
    return S

MATRICES = {"c1": wave1_specs, "era": era_specs, "survival": survival_specs,
            "meta": meta_specs}


# ---------------- helpers ----------------
def run_name(camp, spec):
    return f"camp_{camp}_{spec['id']}"

def read_result(name):
    """Summarize a finished run from its best_config.json."""
    p = os.path.join(RUNS, name, "best_config.json")
    if not os.path.exists(p):
        return dict(ok=False, reason="no best_config.json (0 survivors or crash)")
    try:
        b = json.load(open(p))
    except Exception as e:
        return dict(ok=False, reason=str(e))
    out = dict(ok=True, evaluated=b.get("evaluated"))
    for tag, node in (("train_best", b.get("holdout")),
                      ("oos_best", (b.get("holdout_best") or {}).get("holdout"))):
        if node:
            out[tag] = dict(liq=bool(node.get("liq")),
                            growth=node.get("growth"),
                            pct_mo=round(100 * (math.exp(node["growth"]) - 1), 2)
                                   if node.get("growth") is not None else None,
                            maxdd=node.get("maxdd"), n=node.get("n"),
                            tpm=node.get("tpm"),
                            max_hold_days=node.get("max_hold_days"))
    out["oos_rank"] = (b.get("holdout_best") or {}).get("rank")
    out["survivors"] = len(b.get("holdout_survivors") or [])
    return out

def best_holdout(res):
    """Best non-liquidated holdout growth of a result (train-best or OOS-best)."""
    cands = []
    for tag in ("train_best", "oos_best"):
        n = res.get(tag)
        if n and not n["liq"] and n.get("growth") is not None:
            cands.append(n["growth"])
    return max(cands) if cands else None


class Campaign:
    def __init__(self, name, procs, matrix="c1"):
        self.name, self.procs = name, procs
        self.dir = os.path.join(CAMPS, name)
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        self.plan_path = os.path.join(self.dir, "plan.json")
        self.stop_path = os.path.join(self.dir, "STOP")
        self.spaces = {}
        if os.path.exists(self.plan_path):
            self.plan = json.load(open(self.plan_path))
            print(f"resuming campaign '{name}': "
                  f"{sum(1 for s in self.plan['specs'] if s['status']=='done')}"
                  f"/{len(self.plan['specs'])} specs done", flush=True)
        else:
            gen = MATRICES.get(matrix, wave1_specs)
            self.plan = dict(name=name, created=time.strftime("%Y-%m-%d %H:%M"),
                             procs=procs, specs=gen(), wave=1, matrix=matrix,
                             note="wave-2/3 specs are appended automatically")
            self.save()
        self._make_spaces()

    def save(self):
        self.plan["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        json.dump(self.plan, open(self.plan_path, "w"), indent=1)

    def _make_spaces(self):
        base = os.path.join(HERE, "param_space.json")
        sdir = os.path.join(self.dir, "spaces")
        os.makedirs(sdir, exist_ok=True)
        self.spaces = {
            "default": base,
            "tight": make_space_variant(base, os.path.join(sdir, "tight.json"),
                                        tight=True),
            "ratchet_both": make_space_variant(base, os.path.join(sdir, "ratchet_both.json"),
                                               tight=False, rocx_tslm=[0, 1]),
            "ratchet_only": make_space_variant(base, os.path.join(sdir, "ratchet_only.json"),
                                               tight=False, rocx_tslm=[1]),
            "tight_ratchet": make_space_variant(base, os.path.join(sdir, "tight_ratchet.json"),
                                                tight=True, rocx_tslm=[0, 1]),
            "levsafe": make_space_variant(base, os.path.join(sdir, "levsafe.json"),
                                          tight=True, lev_cap=4, sl_cap=0.04),
            "levsafe6": make_space_variant(base, os.path.join(sdir, "levsafe6.json"),
                                           tight=True, lev_cap=6, sl_cap=0.06),
            "levsafe_ratchet": make_space_variant(base, os.path.join(sdir, "levsafe_ratchet.json"),
                                                  tight=True, lev_cap=4, sl_cap=0.04,
                                                  rocx_tslm=[1]),
        }

    def stopped(self):
        return os.path.exists(self.stop_path)

    # ---------------- execution ----------------
    def spec_cmd(self, spec):
        name = run_name(self.name, spec)
        if spec.get("kind") == "backtest":
            return [sys.executable, "backtest_cli.py",
                    "--config", spec["config"], "--name", spec["bt_name"]]
        if spec.get("kind") == "metax":
            return [sys.executable, "metax_cli.py", "--mode", spec["mode"],
                    "--buckets", spec["method"], "--name", name,
                    "--total", str(spec.get("total", 30000))]
        if spec.get("kind") == "metax_refine":
            return [sys.executable, "metax_cli.py",
                    "--refine", f"runs/{spec['run_override']}",
                    "--iters", str(spec.get("iters", 400))]
        cmd = [sys.executable, "optimize2_cli.py",
               "--strategy", spec["strategy"], "--mode", spec["mode"],
               "--method", spec["method"], "--algo", spec.get("algo", "genetic"),
               "--procs", str(self.procs), "--batch", "100",
               "--total", str(spec["total"]),
               "--gap-mode", "skip_contaminated",
               "--max-hold-days", str(spec.get("mh", 5)),
               "--scoring", spec.get("scoring", "classic"),
               "--space", self.spaces.get(spec.get("space", "default"),
                                          self.spaces["default"]),
               "--name", name]
        # window: alternating-block holdout OR train-end date (default TRAIN_END)
        if spec.get("hold"):
            cmd += ["--holdout-days", str(spec["hold"])]
        else:
            te = spec.get("train_end", TRAIN_END)
            if te:
                cmd += ["--train-end", te]
        if spec.get("train_start"):
            cmd += ["--train-start", spec["train_start"]]
        if spec.get("dd"):
            cmd += ["--max-dd", str(spec["dd"])]
        if spec.get("resume_from"):
            cmd += ["--resume-from", spec["resume_from"]]
            if spec.get("merge_mode"):
                cmd += ["--merge-mode", spec["merge_mode"]]
        cmd += spec.get("extra") or []
        return cmd

    def run_spec(self, spec):
        name = spec.get("run_override") or run_name(self.name, spec)
        cmd = self.spec_cmd(spec)
        logp = os.path.join(self.dir, "logs", spec["id"] + ".log")
        print(f"[{time.strftime('%H:%M:%S')}] {spec['id']}: {' '.join(cmd[1:])}",
              flush=True)
        spec["status"] = "running"
        spec["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        with open(logp, "a") as lf:
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=lf,
                                    stderr=subprocess.STDOUT)
            interrupted = False
            while proc.poll() is None:
                time.sleep(5)
                if self.stopped() and not interrupted:
                    print("STOP requested — asking the run to finalize "
                          "gracefully…", flush=True)
                    proc.send_signal(signal.SIGINT)   # optimize2 finalizes
                    interrupted = True
        rc = proc.returncode
        if spec.get("kind") == "backtest":
            spec["status"] = "done" if rc == 0 else "failed"
        else:
            spec["result"] = read_result(name)
            spec["status"] = ("interrupted" if interrupted else
                              "done" if rc == 0 else "failed")
        spec["rc"] = rc
        self.save()
        r = spec.get("result") or {}
        ob = r.get("oos_best") or r.get("train_best")
        tag = "OOS-best" if r.get("oos_best") else "holdout"
        print(f"    -> {spec['status']}"
              + (f" | {tag} {ob['pct_mo']:+.1f}%/mo dd {100*ob['maxdd']:.0f}% "
                 f"tpm {ob['tpm']:.1f}" if ob and not ob["liq"]
                 and ob.get("pct_mo") is not None else
                 " | no surviving candidate"), flush=True)

    # ---------------- iteration (wave 2/3 generation) ----------------
    def gen_wave2(self):
        if self.plan.get("matrix") == "meta":
            # phase 2 = joint refine of component params, frozen assignment
            # (idempotent: re-runs of fixed wave-1 specs get their refine later)
            have = {s["id"] for s in self.plan["specs"]}
            new = []
            for s in self.plan["specs"]:
                if s["wave"] == 1 and s["status"] == "done" \
                        and (s.get("result") or {}).get("ok") \
                        and f"w2_refine_{s['id']}" not in have:
                    tb = (s["result"].get("train_best")
                          or s["result"].get("oos_best"))
                    if tb and not tb.get("liq") and (tb.get("growth") or 0) > 0:
                        new.append(dict(id=f"w2_refine_{s['id']}", wave=2,
                                        kind="metax_refine", status="pending",
                                        strategy="metax", mode=s["mode"],
                                        method=s["method"], iters=400,
                                        run_override=run_name(self.name, s)))
            self.plan["specs"] += new
            self.plan["wave"] = 2
            self.save()
            print(f"wave 2 (meta): {len(new)} joint-refine specs", flush=True)
            return new
        done = [s for s in self.plan["specs"] if s["wave"] == 1 and s["status"] == "done"]
        scored = []
        for s in done:
            g = best_holdout(s.get("result") or {})
            if g is not None and g > 0:
                scored.append((g, s))
        scored.sort(key=lambda x: -x[0])
        new = []
        # refine the top 6 positives
        for g, s in scored[:6]:
            new.append(dict(id=f"w2_ref_{s['id'][3:]}", wave=2, status="pending",
                            strategy=s["strategy"], mode=s["mode"], method=s["method"],
                            scoring=s.get("scoring", "classic"), space=s["space"],
                            total=50000, mh=s.get("mh", 5), dd=s.get("dd"),
                            hold=s.get("hold"), train_start=s.get("train_start"),
                            train_end=s.get("train_end", TRAIN_END), algo="refine",
                            resume_from=f"runs/{run_name(self.name, s)}"))
        # breed-merge groups of >=2 positives with identical genome keys
        groups = {}
        for g, s in scored:
            groups.setdefault((s["strategy"], s["mode"], s["method"]), []).append(s)
        era = self.plan.get("matrix") in ("era", "survival")
        for (strat, mode, meth), members in groups.items():
            if len(members) >= 2:
                rf = ",".join(f"runs/{run_name(self.name, m)}" for m in members[:4])
                new.append(dict(id=f"w2_merge_{strat}_{mode}_{meth}", wave=2,
                                status="pending", strategy=strat, mode=mode,
                                method=meth, scoring="classic", space="default",
                                total=100000, mh=5, dd=0.55, algo="genetic",
                                # era campaigns gate merges on cross-era blocks
                                hold=(21 if era else None),
                                train_end=(None if era else TRAIN_END),
                                resume_from=rf, merge_mode="breed"))
        self.plan["specs"] += new
        self.plan["wave"] = 2
        self.save()
        print(f"wave 2: {len(new)} specs generated "
              f"({sum(1 for n in new if n.get('merge_mode'))} breed-merges)", flush=True)
        return new

    def gen_wave3(self):
        if self.plan.get("matrix") == "meta":
            self.plan["wave"] = 3   # metax publishes its own full backtests
            self.save()
            return []
        new = []
        for s in self.plan["specs"]:
            if s["wave"] in (1, 2) and s["status"] == "done" and s.get("result", {}).get("ok"):
                ob = s["result"].get("oos_best")
                if ob and not ob["liq"] and (ob.get("growth") or 0) > 0:
                    name = run_name(self.name, s)
                    cfg = os.path.join("runs", name, "holdout_best_config.json")
                    if os.path.exists(os.path.join(HERE, cfg)):
                        new.append(dict(id=f"w3_bt_{s['id']}", wave=3, status="pending",
                                        kind="backtest", config=cfg,
                                        bt_name=f"{name}_oosbest_full"))
        self.plan["specs"] += new
        self.plan["wave"] = 3
        self.save()
        print(f"wave 3: {len(new)} full backtests of surviving OOS-bests", flush=True)
        return new

    # ---------------- report ----------------
    def report(self):
        lines = [f"# Campaign {self.name} — report",
                 f"updated {time.strftime('%Y-%m-%d %H:%M')}",
                 "",
                 "Ranked by OOS-best holdout %/mo (the honest number). "
                 "tpm = trades/month; prefer high tpm + modest %/trade "
                 "(many-small-gains goal). Verify with walk-forward before adopting.",
                 "",
                 "| rank | spec | strat | mode | method | scoring | space | holdout %/mo | dd | tpm | mh(d) |",
                 "|---|---|---|---|---|---|---|---|---|---|---|"]
        scored = []
        for s in self.plan["specs"]:
            if s.get("kind") == "backtest" or s["status"] != "done":
                continue
            r = s.get("result") or {}
            ob = r.get("oos_best") or r.get("train_best")
            if ob and not ob["liq"] and ob.get("pct_mo") is not None:
                scored.append((ob["pct_mo"], s, ob))
        scored.sort(key=lambda x: -x[0])
        for k, (pct, s, ob) in enumerate(scored, 1):
            lines.append(f"| {k} | {s['id']} | {s['strategy']} | {s['mode']} | "
                         f"{s['method']} | {s.get('scoring','')} | {s.get('space','')} | "
                         f"{pct:+.1f}% | {100*(ob.get('maxdd') or 0):.0f}% | "
                         f"{ob.get('tpm') or 0:.1f} | {ob.get('max_hold_days') or 0:.1f} |")
        dead = [s["id"] for s in self.plan["specs"]
                if s["status"] == "done" and not s.get("kind")
                and best_holdout(s.get("result") or {}) in (None,)]
        lines += ["", f"No survivors / negative holdout: {', '.join(dead) or 'none'}"]
        open(os.path.join(self.dir, "report.md"), "w").write("\n".join(lines))
        print("report written:", os.path.join(self.dir, "report.md"), flush=True)

    # ---------------- main loop ----------------
    def run(self):
        if self.stopped():
            os.remove(self.stop_path)   # starting/resuming clears STOP
        while True:
            pending = [s for s in self.plan["specs"]
                       if s["status"] in ("pending", "interrupted", "failed_retry")]
            if not pending:
                if self.plan.get("matrix") == "meta":
                    if self.gen_wave2():
                        continue
                    break
                cur = self.plan.get("wave", 1)
                if cur == 1 and not any(s["wave"] == 2 for s in self.plan["specs"]):
                    if self.gen_wave2():
                        continue
                    self.plan["wave"] = 2
                if cur <= 2 and not any(s["wave"] == 3 for s in self.plan["specs"]):
                    if self.gen_wave3():
                        continue
                    self.plan["wave"] = 3
                break
            self.run_spec(pending[0])
            self.report()   # keep the report fresh after every experiment
            if self.stopped():
                print("campaign stopped — rerun campaign.py (or press Resume "
                      "in the panel) to continue", flush=True)
                self.save()
                return
        self.report()
        print("campaign complete.", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="c1")
    ap.add_argument("--procs", type=int, default=14)
    ap.add_argument("--matrix", default="c1", choices=sorted(MATRICES),
                    help="which spec matrix a NEW campaign starts with "
                         "(existing campaigns keep their plan)")
    ap.add_argument("--wait-for", default=None,
                    help="poll another campaign until every spec is finished, "
                         "then start (queue campaigns without oversubscribing)")
    args = ap.parse_args()
    if args.wait_for:
        other = os.path.join(CAMPS, args.wait_for, "plan.json")
        print(f"waiting for campaign '{args.wait_for}' to finish…", flush=True)
        while True:
            try:
                specs = json.load(open(other)).get("specs", [])
                busy = [s for s in specs
                        if s["status"] in ("pending", "running", "interrupted")]
                if not busy:
                    break
            except Exception:
                pass
            time.sleep(60)
        print(f"'{args.wait_for}' finished — starting '{args.name}'", flush=True)
    Campaign(args.name, args.procs, args.matrix).run()
