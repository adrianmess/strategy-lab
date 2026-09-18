#!/usr/bin/env python3
"""Build re-cost shards: every published backtest whose fee basis is STALE,
with its genome embedded so an off-box worker (the MacBook) needs no access
to optimizer/runs.

Why this exists: until 2026-09-17 the stack believed MEXC's public
contract/detail fee rates, which describe the WEB schedule (0% maker, 0-2bp
taker). API trading has its own dearer schedule — 0.06% maker / 0.08% taker,
effective 2026-06-01 — and it overrides web rates and every promo. So every
entry published before the correction was simulated at a fraction of what the
account actually pays, and for a high-frequency genome the fee IS the result.

An entry is stale when it carries no fee_per_side at all (published before the
field existed) or one below today's rate for its coin and mode. That makes the
whole job resumable: re-run this after a batch finishes and only what's still
wrong comes back.

Run ON THE MINI (it owns backtests.js and the runs tree):
  ~/venv/bin/python3 scripts/build_fee_shard.py --shards 4
  -> dashboard/bt_refresh/fee_shard_01.json ... _04.json

Then on the MacBook, per shard:
  PANEL_KEY=<key> python3 scripts/refresh_backtests_worker.py \
      --shard fee_shard_01.json --procs 8 \
      --hub http://admns-Mac-mini.local:8800
"""
import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), ".."))
OPT = os.path.join(REPO, "optimizer")
sys.path.insert(0, os.path.join(REPO, "adaptive_trader", "research2"))

ROUTERS = {"metax", "metax2", "pairx", "fcfsx"}   # no single genome to re-run
_SUF = ["_oosbest_full", "_best_full", "_full"]
_GEN = {"_oosbest_full": "holdout_best_config.json",
        "_best_full": "best_config.json", "_full": "best_config.json"}


def genome_of(e):
    """The candidate this entry simulated: inline if present, else from the
    source run's config file."""
    g = e.get("config")
    if g:
        return g
    name = e.get("name") or ""
    for s in _SUF:
        if name.endswith(s):
            r = name[:-len(s)]
            gp = os.path.join(OPT, "runs", r, _GEN[s])
            if not os.path.exists(gp):
                gp = os.path.join(OPT, "runs", r, "best_config.json")
            return json.load(open(gp)) if os.path.exists(gp) else None
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shards", type=int, default=1,
                    help="split across N shard files (one per worker process "
                         "group / machine)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the total items (0 = everything stale)")
    ap.add_argument("--mode", choices=["lev", "spot", "both"], default="both")
    ap.add_argument("--tf", default="all",
                    help="comma-separated timeframes to include (e.g. 1,3) "
                         "or 'all'")
    ap.add_argument("--min-trades", type=int, default=0,
                    help="only entries with at least this many trades — the "
                         "high-frequency ones are where the fee matters most")
    ap.add_argument("--out-dir", default=os.path.join(REPO, "dashboard",
                                                      "bt_refresh"))
    ap.add_argument("--prefix", default="fee_shard")
    ap.add_argument("--fee-side", choices=["taker", "maker", "both"],
                    default="both",
                    help="taker = market fills (the conservative number, and "
                         "the primary stats either way); maker = resting "
                         "limits; both = run each entry twice and carry the "
                         "maker result alongside as a bracket. 'both' doubles "
                         "the runtime.")
    ap.add_argument("--all", action="store_true",
                    help="include entries whose fee is already current "
                         "(a full re-run rather than only the stale ones)")
    a = ap.parse_args()

    try:
        from fees_live import per_side
    except Exception as e:
        sys.exit(f"cannot import fees_live ({e}) — run this on the mini")

    txt = open(os.path.join(REPO, "dashboard", "backtests.js")).read()
    entries = json.JSONDecoder().raw_decode(
        txt[txt.index("=") + 1:].lstrip())[0]

    tfs = None if a.tf == "all" else {t.strip().rstrip("m")
                                      for t in a.tf.split(",") if t.strip()}
    items = []
    skipped = {"router": 0, "no_genome": 0, "current": 0, "filtered": 0}
    for e in entries:
        if e.get("strategy") in ROUTERS:
            skipped["router"] += 1
            continue
        mode = e.get("mode")
        if mode not in ("lev", "spot"):
            skipped["filtered"] += 1
            continue
        if a.mode != "both" and mode != a.mode:
            skipped["filtered"] += 1
            continue
        tf = str(e.get("timeframe") or "").rstrip("m")
        if tfs is not None and tf not in tfs:
            skipped["filtered"] += 1
            continue
        st = e.get("stats") or {}
        if a.min_trades and (st.get("n") or 0) < a.min_trades:
            skipped["filtered"] += 1
            continue
        coin = (e.get("pair") or "").split("_")[0]
        taker = per_side(mode, coin, "taker")
        maker = per_side(mode, coin, "maker")
        want = maker if a.fee_side == "maker" else taker
        have = e.get("fee_per_side")
        # 1e-9 guards float noise; anything at or above today's rate is fine
        if not a.all and have is not None and float(have) >= want - 1e-9:
            skipped["current"] += 1
            continue
        g = genome_of(e)
        if not g:
            skipped["no_genome"] += 1
            continue
        item = dict(name=e["name"], pair=e.get("pair"), timeframe=tf,
                    mode=mode, method=e.get("method"),
                    strategy=e.get("strategy"), kind=e.get("kind"),
                    opt=e.get("opt"), genome=g,
                    fee_was=have, fee_now=want,
                    fee_side=("maker" if a.fee_side == "maker" else "taker"))
        if a.fee_side == "both" and abs(maker - taker) > 1e-12:
            item["fee_alt_rate"] = maker
            item["fee_alt_side"] = "maker"
        items.append(item)

    # heaviest first so a shard's long pole starts early and the tail is short
    items.sort(key=lambda x: -((x.get("opt") or {}).get("evaluated") or 0))
    if a.limit:
        items = items[:a.limit]

    os.makedirs(a.out_dir, exist_ok=True)
    n = max(1, a.shards)
    paths = []
    for k in range(n):
        part = items[k::n]          # round-robin keeps the shards balanced
        p = os.path.join(a.out_dir, f"{a.prefix}_{k + 1:02d}.json")
        json.dump(part, open(p, "w"), default=float)
        paths.append((p, len(part)))

    print(f"stale entries needing a re-cost: {len(items)}")
    print(f"  skipped: {skipped['current']} already current, "
          f"{skipped['router']} routers (use the combo re-run), "
          f"{skipped['no_genome']} without a genome, "
          f"{skipped['filtered']} filtered out")
    for p, k in paths:
        print(f"  {p}  ({k} items)")
    if items:
        per = 6.8 * (2 if a.fee_side == "both" else 1)
        secs = per * len(items)
        print(f"\nfee basis: {a.fee_side}"
              + (" (each entry simulated twice)" if a.fee_side == "both" else ""))
        print(f"~{secs / 3600:.1f} core-hours total (~{per:.1f}s each); "
              f"at 8 procs {secs / 3600 / 8:.1f}h, "
              f"at 12 procs {secs / 3600 / 12:.1f}h")


if __name__ == "__main__":
    main()
