# Strategy Lab — Session Handoff
Updated: 2026-08-02 (EC2 offload live). Paste into a new session to resume. Keep this file updated as work progresses.

## CLOSED: EC2 gamut offload (2026-08-02 → 2026-08-09)
FULLY TORN DOWN 2026-08-09 (verified via CloudShell sweep: 0 fleets, 0 instances, 0 spot requests, 0 EIPs, 0 EBS volumes, 0 S3 buckets, IAM role gamut-box deleted, launch template gamut-A deleted, AMI ami-041e982838c990d16 + snap-0660b53012508ec22 deregistered/deleted, all monitor/launcher scheduled tasks deleted, Mac sync loops stopped). NOTHING BILLABLE REMAINS.
Results: main 12,960/12,960 + hype 2,592/2,592 durable on the Mac. Rebuild guide: docs/EC2_OFFLOAD_RUNBOOK.md (+ docs/ec2/box_snapshot.txt).

## ARCHIVED DETAIL (historical): EC2 gamut offload (2026-08-02)
- Campaign gamut_g0801_2122 (12,960 specs; 445 done locally; ~12,515 remaining) OFFLOADED to AWS. Local gamut STOPPED (STOP file). Plan repaired: dup-date bug '2024-11-16&#50;024-11-16' fixed in 3,240 cmds, 148 failed reset (UI now clamps dates).
- Instance: BOX A PENDING RELAUNCH (monitor 2026-08-05 ~14:20 PDT): old c7a.48xlarge i-016786d5b788d3a12 (us-east-2c) was spot-stopped 13:35 PDT; during authorized AZ move its request sir-mzrqhjkq was cancelled + instance terminated, but ALL 48xlarge types (c8a/c7a/c6a) were dry in 2a, 2b AND 2c. Fallback: open PERSISTENT spot request **sir-tqkzgchn** (c7a.48xlarge, 2c, stop-on-interrupt) — fulfills when capacity returns. NEXT MONITOR RUN: if fulfilled, associate EIP 3.20.175.238 (eipalloc-0d7a1245b58d910ba, --allow-reassociation) to the new instance and re-arm offload_watch.sh with ec2_boot_workers.sh. 17 in-flight specs lost (accepted). Elastic IP 3.20.175.238, persistent spot + stop-on-interrupt, 200GB gp3, SG gamut-ssh (sg-0b1b15deb810aa8ad, SSH only from 97.120.254.99). Key: ~/Downloads/gamut-key.pem.
- On the box: repo at ~/strategy-lab (no 36GB caches — rebuilt on demand from 590MB data/), venv at ~/venv (numpy 2.4.6/pandas 3.0.5/numba 0.66/pyarrow 24.0), worker in tmux session 'gamut': `gamut_worker.py --jobs 13` (13×14 procs), log ~/worker.log, per-spec logs in campaigns/.../logs/. All 16 AI space variants synced. Worker fixes applied: local python + local AI-space path resolution (plans embed builder-machine paths).
- Mac side: sync loop running (scripts/offload_sync.sh, /tmp/offload_sync.log) — pulls completed runs + worker_state.json every 5 min, additive only. Monitor: `python3 -c "import json,collections;print(collections.Counter(v['status'] for v in json.load(open('optimizer/campaigns/gamut_g0801_2122/worker_state.json')).values()))"`.
- Graceful stop: `ssh -i ~/Downloads/gamut-key.pem ubuntu@3.20.175.238 'touch strategy-lab/optimizer/campaigns/gamut_g0801_2122/STOP_WORKER'`.
- BOX B (2026-08-04; MOVED to us-east-2b 2026-08-05 ~14:15 PDT by monitor): c7a.24xlarge **i-0348810b392af18ff**, us-east-2b, Elastic IP 18.117.41.39, ~$1.43/hr, EIP re-associated, workers re-armed 14:21 (tmux gamut+keeper up, cron installed). Prior 2c instance i-03cdd4c4b1ef62f78 (sir-ae2zksxn) cancelled+terminated; 2a had no 24xl capacity, 2b c8a dry → c7a.24xlarge landed. NOTE: this was B's SECOND cancel+relaunch today — an earlier run's move attempt failed back into 2c and logged 'skip further attempts today', which this run overrode (missed the note; in-flight loss accepted, box is now out of the interrupting 2c). No further moves for either box today. moved B on 2026-08-05. — runs the MAIN plan in REVERSE (jobs=6, meet-in-middle with box A). Spot quota now 300 vCPU (288 used). Sync loop covers both boxes (pull runs + push done-markers both ways); Progress page merges worker_state*.json.
- NEW ARCHITECTURE (2026-08-05 15:30): Box A = EC2 FLEET fleet-a4ea48c4-7e52-47e0-85db-f9fdf0288962 (maintain, capacity-optimized, c8a/c7a/c6a.48xl x 3 AZs; currently unfulfilled — regional drought). Fleet instances SELF-ARM via launch-template gamut-A user-data: tag, associate EIP 3.20.175.238, pull code+markers from s3://gamut-sync-637309463295, cron, start workers. S3 RENDEZVOUS: IAM role gamut-box (instance profile) on boxes; box_s3_push.sh cron (*/5) pushes runs/state/backtests to the bucket; code mirrored at s3://.../code/. Boot scripts now session-guarded + dynamic jobs (nproc/11). Teardown additions: delete fleet, delete launch template gamut-A, empty+delete bucket, delete IAM role/profile gamut-box.
- SPOT-INTERRUPTION RESILIENCE (2026-08-03): interrupted 06:59 PDT (capacity-not-available in 2b; request sir-9pdqhysq relaunches automatically). Elastic IP 3.20.175.238. Mac watcher (scripts/offload_watch.sh) re-arms workers+sync when box returns; cron @reboot on box self-starts workers. Hourly scheduled task 'ec2-gamut-monitor' reports status. AMI **ami-041e982838c990d16** (gamut-worker-20260803) = full env+data+campaigns snapshot → can relaunch in ANY AZ/type in ~15 min (push runs/ markers from Mac before starting worker so post-AMI completions aren't redone).
- MAIN CAMPAIGN COMPLETE (verified 2026-08-08 ~02:45 PDT): all 12,960 g0801_2122 specs have durable results (best_config or no_survivor) — 0 missing. Both main workers exited cleanly. ONLY gamut_ghype remains: box A forward jobs=17 (full width since 05:13 UTC), box B REVERSE jobs=8 (started 09:42 UTC 2026-08-08, boot script b updated+shipped). ~600/2,592 hype done at that point; 17 old 'failed' hype specs auto-retry. WHEN HYPE COMPLETES: teardown checklist below (ask Adrian first). The 384-vCPU quota case is now moot — if it clears, no new capacity needed.
- HYPE STARTED EARLY (user request 2026-08-07 ~18:00 PDT): gamut_ghype worker now runs ALONGSIDE main on box A — tmux session 'hype', `--jobs 5` (243/2,592 already done at start). ec2_boot_workers.sh updated: hype session starts immediately (jobs=5 while the main gamut session exists, full width otherwise). MONITOR: once the MAIN campaign completes on A, kill the hype session and restart it at full width (`--jobs $(nproc/11)`) if it's still running at 5 (boot-race can leave it small). No hype worker on box B (main-reverse only).
- TEARDOWN WHEN DONE (Adrian or with his OK): cancel the PERSISTENT spot request FIRST, then terminate instance, else it relaunches: `aws ec2 describe-spot-instance-requests` → cancel-spot-instance-requests → terminate-instances i-016786d5b788d3a12. EBS deletes on termination. Also release the Elastic IP and deregister the AMI + its snapshot when fully done.

## 1. Goal
Personal platform (`/Users/adrian/Code/strategy-lab`) for optimizing, backtesting, and live-trading TradingView-ported strategies on MEXC (native API — Playwright era is over). Current focus: SPOT, multi-pair (SOL/BTC/ETH/DOGE/XRP/SUI vs USDT), one-position-at-a-time FCFS. Core doctrine: HONEST evaluation — holdout/walk-forward beats train-best; only walk-forward PASS runs may be adopted to live.

**Standing rules (do not re-litigate):**
- Claude NEVER places trades or flips live — Adrian does all irreversible financial actions.
- `/Users/adrian/Code/mexc-td-enhanced` is READ-ONLY.
- Committing + pushing to git is authorized and routine.
- Research-only artifacts (metax2, pairx, non-3m before soak) are blocked from live adoption; the panel enforces this.
- Spot runs use spot candles, lev uses perp; pre-existing runs left as legacy perp.

## 2. Current state
**Live (unchanged):** instance #2 spot router `config_camp_c4_m_spot_vol3` (live assign: low-vol prime #34, mid flat, high-vol v7 #39); instance #3 lev router — both via native MEXC API. Best honest spot result remains SOL stack +13.2%/mo.

**Just completed (commit 9457309, pushed):** Gamut runner upgrades —
- `totals` is a sweep dimension in `gamut.py build_plan` (each combo runs once per evals budget; names suffixed `_t50k`/`_t100k` only when >1 value; legacy single `total` configs still work — verified by 4-spec and legacy smoke tests).
- UI: Max DD (`gmChips_dd`), Max days in trade (`gmChips_mh`), Evals per run (`gmChips_total`), and alternating-holdout block days (`gmChips_alt`, replaces old comma input `gmHoAltV`) are chip editors (`GM_LISTS` state, `gmAdd/gmDel/gmRender`, localStorage key `gmLists`). Run count/time estimate account for all budgets; estimate calibrated from 309 actual runs (median 56k evals/min tf3, 32k tf1 at 14 procs; cadapt ~142/min), tf-aware, scales with procs, shows p25–p75 range.
- AI auto-name: empty or literal "auto" name → `/api/gamut/start` calls ai_advisor `get_key()/call_claude()` for a slug, falls back to `g<timestamp>`; duplicate names get `_2` suffix instead of silently resuming a different config's plan; endpoint returns `name`, client sets `GAMUT_NAME` + fills the field so Stop/polling work.

**Recently completed (this arc):** gamut info-click fix (root cause: global `.field label::after ⓘ` CSS on labels wrapping checkboxes; all options unwrapped to `<input> <span class="optx" data-x=group>`; click handler preventDefault); provenance stories (`gen_story.py`, `/api/story`, 📖 buttons); param-space variants (imported/AI tabs, `gen_ai_spaces.py`); per-pair per-tf spaces (17 files in `param_spaces/`); 1m/3m/5m multi-timeframe (engines `_tf1_extend`, `bph` arg, live trader `timeframe` cfg); replication of top-10 spot+lev across pairs × {3m,1m}; MetaX2/PairX routers (research-only); 5 holdout modes (after/before/between/outside/alternating); combine hard-requirement enforcement at 3 layers (backtests-page filtering, Arm-time re-assert, launch-time verification).

**Honestly rejected (don't revisit without new evidence):** cadapt (c6: 9/10 cells worse); FCFS multi-pair basket (+6.8% vs SOL stack +13.2% — weak alt edges dilute).

## 3. Key decisions
- Dataset selection is env-driven: `LAB_COIN`/`LAB_MARKET`/`LAB_TF`/`LAB_DATA_PINNED`; caches per (coin,market,tf) under `optimizer/cache/<coin>/spotdata/tfN/`; in-process mixing guard raises on dataset mismatch.
- Provenance stamped on every run: pair, market_data, timeframe, holdout_*, launch.json command chains with resume_from.
- Router honesty tiers (train-flavored merge < holdout < causal walk-forward) shown as UI pills; only causal WF PASS is adoptable.
- AI param spaces mine min/max of actually-used indicator values (RSI/BB%B/z/MACD-raw/cd-pct only — SL/TP/durations/leverage are Adrian's manual ranges).
- Gamut run naming: `{name}_{coin}{tf}m_{strat}_{algo:3}_{mode:1}_{method}_{scoring:2}_d{dd}_m{mh}_{hcode}{_tNk}`, capped at 78 chars.
- Multi-budget evals doubles as a convergence check (100k not beating 50k ⇒ compute isn't the bottleneck).

## 4. Files touched (this arc)
- `optimizer/gamut.py` — build_plan totals dimension, `_t{N}k` name code.
- `panel/server.py` — `/api/gamut/start`: totals in required keys, AI naming block, `_2` dedupe loop, returns `name`. Also `/api/gamut/stop|status`, `/api/story`, `/api/param_space` variants, `/api/jobs/router`.
- `dashboard/optimize.html` — chip editors + GM_LISTS JS, gamutCfg (`totals`, name treats "auto" as empty), gamutCount, gamutStart uses `r.name`; guide entries gmevals/gmdd/gmmh/gmname updated.
- Earlier: `optimizer/gen_story.py`, `gen_ai_spaces.py`, `gen_pair_spaces.py`, `metax_cli.py`, `metax2_cli.py`, `pairx_cli.py`, `replicate_top.py`, `optimize2_cli.py`; `adaptive_trader/research/fetch_pair.py`, `gen_5min.py`, `common.py`, `regimes.py`, `research2/wf2.py`, `research2/optimizer2.py`, engines (engine3/scalp_engine/rocx/macdx); `adaptive_trader/trader.py`, `data_feed.py`; `dashboard/backtests.html`, `docs.html`.

## 5. Open issues / blockers
- `ANTHROPIC_API_KEY` not found by `get_key()` — AI naming/stories/AI-space LLM pass fall back to deterministic until Adrian adds it to `.env`.
- `dashboard/backtests.js` ~65MB — GitHub warns every push (100MB hard limit). Offered fix: publish-time caps + periodic pruning. Not done.
- rocx-lev +18.2% replication had suspicious max-hold 20.1d past the ≤7d gate — flagged, never investigated.
- Multi-pair live trader (FCFS slot) + live adapters for metax2/pairx — deferred (basket lost to SOL stack).
- Chip UI + AI naming not yet exercised in a real browser (only static JS syntax check + build_plan smoke test).

## 6. Next steps
1. Adrian: reload Optimize page, sanity-check chip editors, launch a small gamut (1 pair × 2 evals budgets) to exercise the new path end-to-end.
2. Adrian: add `ANTHROPIC_API_KEY` to `.env` to activate AI naming/stories/AI spaces.
3. Investigate the rocx-lev 20.1d max-hold gate violation.
4. Decide on backtests.js size mitigation (publish-time cap on curve/trade points).
5. When a gamut finishes: review `campaigns/gamut_<name>/report.md`; promote winners through causal walk-forward before any adoption talk.
