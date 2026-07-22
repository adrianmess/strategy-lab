#!/usr/bin/env python3
"""Periodic ROUTER re-assignment — the live counterpart of the walk-forward's
per-fold selection.

The chronological walk-forward validated a PROCESS, not a frozen bucket-map:
every 42 days, re-pick which component owns each market bucket using only past
data (risk-averse selector: tight past-DD gate + DD-penalized classic score).
That process scored +26%/mo (lev vt9) and +13%/mo (spot vt9) chained OOS.
This script applies the same selector to a live router config.

Safety rules:
  - refuses to touch a config whose trader is IN A TRADE (position or mirror
    in its state file) — re-assignment happens flat, or not at all;
  - the new assignment must pass the same feasibility gates the WF used; if
    nothing passes, the old assignment stays;
  - every change is backed up and appended to reassign_history.json;
  - the RUNNING trader hot-reloads the changed config at the next bar close
    (also only while flat) — no restart needed.

Usage:
  python3 metax_reassign.py --config config_camp_c4_m_spot_vt9.json   # one
  python3 metax_reassign.py --all                                     # every router config
  python3 metax_reassign.py --all --loop        # daemon: check daily, act on 42d cadence
"""
import _bootstrap as B
import argparse, glob, json, os, shutil, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
AT = os.path.join(os.path.dirname(HERE), "adaptive_trader")
CADENCE_DAYS = 42

from metax_cli import bucket_arrays, build_tables, search_window, RUNS  # noqa


def _flat(cfg):
    try:
        st = json.load(open(os.path.join(AT, cfg.get("state_file",
                                                     "trader_state.json"))))
    except Exception:
        return True
    return not st.get("position") and not st.get("mirror")


def reassign(cfg_name, force=False):
    path = os.path.join(AT, os.path.basename(cfg_name))
    cfg = json.load(open(path))
    cand = cfg.get("candidate") or {}
    if cand.get("strategy") != "metax":
        print(f"{cfg_name}: not a router config — skipped", flush=True)
        return False
    last = cand.get("reassigned_at", cfg.get("adopted_from", {}).get("at", ""))
    if not force and last:
        try:
            age_d = (time.time() - time.mktime(
                time.strptime(last[:16], "%Y-%m-%d %H:%M"))) / 86400
            if age_d < CADENCE_DAYS:
                print(f"{cfg_name}: last (re)assignment {age_d:.0f}d ago "
                      f"(< {CADENCE_DAYS}d) — skipped", flush=True)
                return False
        except Exception:
            pass
    if not _flat(cfg):
        print(f"{cfg_name}: trader is IN A TRADE — re-assignment deferred",
              flush=True)
        return False
    mode, buckets = cfg["mode"], cand["buckets"]
    comps = []
    for c in cand["components"]:
        fn = c.get("file")
        if not fn:   # configs adopted before 'file' was carried through
            fn = ("holdout_best_config.json" if os.path.exists(
                os.path.join(RUNS, c["run"], "holdout_best_config.json"))
                else "best_config.json")
        comps.append(dict(run=c["run"], file=fn, strategy=c["strategy"],
                          path=os.path.join(RUNS, c["run"], fn),
                          holdout_pct=0.0))
    times, bucket, n_b = bucket_arrays(buckets)
    kept, tabs = build_tables(comps, times, bucket, mode)
    # build_tables drops liquidating components -> translate kept indexes back
    # to the ORIGINAL component list the live adapter indexes into
    orig_idx = [next(i for i, c in enumerate(cand["components"])
                     if c["run"] == k["run"]) for k in kept]
    # deterministic per-epoch seed: same day -> same answer, reruns are stable
    seed = int(time.time() // (CADENCE_DAYS * 86400))
    rng = np.random.default_rng(seed)
    dd_gate = 0.35 if mode == "spot" else 0.50
    a_kept = search_window(tabs, n_b, len(kept), t_max=np.inf,
                           total=8000, rng=rng, dd_gate=dd_gate)
    if a_kept is None:
        print(f"{cfg_name}: NO feasible assignment on trailing data — "
              f"keeping the current one", flush=True)
        return False
    new_assign = [(orig_idx[a] if a is not None and a >= 0 else -1)
                  for a in a_kept]
    old_assign = list(cand["assign"])
    stamp = time.strftime("%Y-%m-%d %H:%M")
    changed = new_assign != old_assign
    if changed:
        shutil.copy(path, path + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
        cand["assign"] = new_assign
    cand["reassigned_at"] = stamp
    cfg["candidate"] = cand
    json.dump(cfg, open(path, "w"), indent=1)
    hist_p = os.path.join(AT, "reassign_history.json")
    try:
        hist = json.load(open(hist_p))
    except Exception:
        hist = []
    hist.append(dict(at=stamp, config=os.path.basename(path),
                     changed=changed, old=old_assign, new=new_assign,
                     named={str(i): (cand["components"][a]["run"][:40]
                                     if a >= 0 else "—")
                            for i, a in enumerate(new_assign)}))
    json.dump(hist, open(hist_p, "w"), indent=1)
    if changed:
        print(f"{cfg_name}: RE-ASSIGNED {old_assign} -> {new_assign} "
              f"(the running trader hot-reloads at the next flat bar close)",
              flush=True)
    else:
        print(f"{cfg_name}: selector confirmed the CURRENT assignment "
              f"(timestamp refreshed)", flush=True)
    return changed


def router_configs():
    out = []
    for p in sorted(glob.glob(os.path.join(AT, "config*.json"))):
        try:
            c = json.load(open(p))
        except Exception:
            continue
        if (c.get("candidate") or {}).get("strategy") == "metax":
            out.append(os.path.basename(p))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="ignore the 42-day cadence check")
    ap.add_argument("--loop", action="store_true",
                    help="daemon: re-check every 6h, act when a config's "
                         "assignment is older than the cadence")
    args = ap.parse_args()
    while True:
        targets = [args.config] if args.config else router_configs()
        if not targets:
            print("no router configs found", flush=True)
        for t in targets:
            try:
                reassign(t, force=args.force)
            except Exception as e:
                print(f"{t}: reassign failed: {e}", flush=True)
        if not args.loop:
            break
        time.sleep(6 * 3600)


if __name__ == "__main__":
    main()
