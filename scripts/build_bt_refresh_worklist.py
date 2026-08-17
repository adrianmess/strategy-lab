#!/usr/bin/env python3
"""Build the worklist for refreshing stale (pre Aug-15 cache-bug) published
backtests: top 20 runs per pair x timeframe x MODE by published %/mo.
Each item embeds the genome config, so workers need no run directories.
Emits two shards weighted for the mini (6 procs) and the MacBook (14 procs).
Run on the mini: ~/venv/bin/python3 build_bt_refresh_worklist.py"""
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
RUNS = os.path.join(REPO, "optimizer", "runs")
SUF = ["_oosbest_full", "_best_full", "_full"]
GENOME = {"_oosbest_full": "holdout_best_config.json",
          "_best_full": "best_config.json", "_full": "best_config.json"}


def runof(n):
    for s in SUF:
        if n.endswith(s):
            return n[:-len(s)], s
    return None, None


txt = open(os.path.join(REPO, "dashboard", "backtests.js")).read()
es = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]

# rank runs per (pair, tf, mode) by their best published %/mo
per_run = {}
for e in es:
    if (e.get("created") or "") >= "2026-08-15 13":
        continue
    r, suf = runof(e["name"])
    if not r:
        continue
    key = (e.get("pair") or "?", str(e.get("timeframe") or "?"),
           e.get("mode") or "?")
    g = (e.get("stats") or {}).get("monthly_growth_pct") or 0
    cur = per_run.setdefault((key, r), {"g": g, "entries": []})
    cur["g"] = max(cur["g"], g)
    cur["entries"].append(e)

groups = collections.defaultdict(list)
for (key, r), v in per_run.items():
    groups[key].append((v["g"], r, v["entries"]))

items, missing = [], 0
for key, lst in sorted(groups.items()):
    for g, r, entries in sorted(lst, reverse=True)[:20]:
        for e in entries:
            _, suf = runof(e["name"])
            # genome: prefer the entry's embedded config; else the run dir
            cfg = e.get("config")
            if not cfg:
                p = os.path.join(RUNS, r, GENOME[suf])
                if not os.path.exists(p):
                    p = os.path.join(RUNS, r, "best_config.json")
                if not os.path.exists(p):
                    missing += 1
                    continue
                cfg = json.load(open(p))
            items.append(dict(
                name=e["name"], pair=e.get("pair"),
                timeframe=str(e.get("timeframe") or ""),
                mode=e.get("mode"), method=e.get("method"),
                strategy=e.get("strategy"),
                kind=e.get("kind"), opt=e.get("opt"), genome=cfg))

# shard by GROUP (cache reuse) weighted ~14:6 macbook:mini
gsorted = collections.defaultdict(list)
for it in items:
    gsorted[(it["pair"], it["timeframe"], it["mode"])].append(it)
mac, mini, macn, minin = [], [], 0, 0
for key, lst in sorted(gsorted.items(), key=lambda kv: -len(kv[1])):
    if macn * 6 <= minin * 14:
        mac += lst
        macn += len(lst)
    else:
        mini += lst
        minin += len(lst)
out = os.path.join(REPO, "dashboard", "bt_refresh")
os.makedirs(out, exist_ok=True)
json.dump(mac, open(os.path.join(out, "shard_macbook.json"), "w"))
json.dump(mini, open(os.path.join(out, "shard_mini.json"), "w"))
print(f"{len(items)} entries to refresh ({missing} skipped, no genome) — "
      f"macbook {len(mac)}, mini {len(mini)}")
