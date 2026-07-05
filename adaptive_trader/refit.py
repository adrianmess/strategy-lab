#!/usr/bin/env python3
"""Auto-refit: refresh data, re-optimize the trader's parameters on all history,
swap them into config.json (with backup) and publish a dashboard backtest.

One-shot:   python3 refit.py                      (uses refit settings in config.json)
Loop:       python3 refit.py --loop               (refits every `refit.days` days)
Options:    --procs 8 --hours 2 --config config_spot.json --dry (don't write config)

Guardrails: the new candidate must be feasible (no liquidation, DD cap) and its
full-history score must beat the current candidate's, else config is unchanged.
"""
import argparse, json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "research"))
sys.path.insert(0, os.path.join(HERE, "research2"))


def refit_once(cfg_path, procs, hours, dry):
    cfg = json.load(open(cfg_path))
    strategy = cfg["candidate"]["strategy"]
    mode, method = cfg["mode"], cfg["method"]

    print("=== 1/4 refreshing data from CoinAPI ===", flush=True)
    subprocess.run([sys.executable, os.path.join(HERE, "research", "update_data.py")],
                   check=True)

    print("=== 2/4 optimizing ===", flush=True)
    name = f"refit_{time.strftime('%Y%m%d_%H%M')}"
    opt = os.path.join(HERE, "..", "optimizer", "optimize_cli.py")
    subprocess.run([sys.executable, opt, "--strategy", strategy, "--mode", mode,
                    "--method", method, "--procs", str(procs),
                    "--hours", str(hours), "--name", name], check=True)
    best_path = os.path.join(HERE, "..", "optimizer", "runs", name, "best_config.json")
    if not os.path.exists(best_path):
        print("no feasible candidate found — keeping current config")
        return False
    best = json.load(open(best_path))

    print("=== 3/4 guardrail comparison ===", flush=True)
    from wf2 import load_globals, eval_config, feasible
    os.chdir(os.path.join(HERE, "..", "optimizer", "runs", name))
    load_globals((strategy,))
    m_new = best["metrics"]
    m_cur = eval_config(cfg["candidate"], method, mode, None, "2099-01-01")
    cur_score = m_cur["score"] if m_cur else -1e9
    print(f"current score {cur_score:.4f} vs new {m_new['score']:.4f}")
    if m_new["score"] <= cur_score:
        print("new candidate does not beat current — keeping current config")
        return False

    print("=== 4/4 swapping config + publishing backtest ===", flush=True)
    if not dry:
        shutil.copy(cfg_path, cfg_path + f".bak.{time.strftime('%Y%m%d_%H%M')}")
        cfg["candidate"] = best["cand"]
        cfg["last_refit"] = time.strftime("%Y-%m-%d %H:%M")
        json.dump(cfg, open(cfg_path, "w"), indent=1)
        print(f"config updated: {cfg_path}")
    bt = os.path.join(HERE, "..", "optimizer", "backtest_cli.py")
    subprocess.run([sys.executable, bt, "--config", best_path, "--name", name],
                   cwd=os.path.join(HERE, "..", "optimizer"))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--procs", type=int, default=None)
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry", action="store_true", help="don't modify config.json")
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    r = cfg.get("refit", {})
    procs = args.procs or r.get("procs", 4)
    hours = args.hours or r.get("hours", 1.0)
    days = r.get("days", 28)
    while True:
        refit_once(args.config, procs, hours, args.dry)
        if not args.loop:
            break
        print(f"sleeping {days} days until next refit...")
        time.sleep(days * 86400)


if __name__ == "__main__":
    main()
