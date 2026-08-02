# Gamut Offload Plan (planning only — nothing built yet)
Updated: 2026-08-02. Goal: cut the ~20-day gamut wall-clock by adding a second Mac (same LAN) and optionally a cloud box, WITHOUT touching the running gamut.

## Why this is safe
`gamut.py execute()` skips any spec whose `optimizer/runs/<name>/best_config.json` exists. So a worker machine that completes runs and rsyncs them back into the primary's `runs/` makes the primary skip those specs automatically. No changes to the running process, its plan.json, or its statuses — workers only ADD run directories.

## Architecture (both routes)
- Primary (this Mac): keeps running the live gamut forwards through plan.json. Untouched.
- Worker: gets a synced READ-ONLY copy of plan.json, executes pending specs in REVERSE order (end of plan backwards), with its own skip-if-done against the synced `runs/`. The two fronts meet in the middle; worst case ~1 duplicated run per worker.
- Sync loop (every ~5 min, both directions, additive only):
  - primary→worker: `rsync -a --ignore-existing primary:optimizer/runs/ worker:optimizer/runs/` + fresh plan.json copy
  - worker→primary: `rsync -a --ignore-existing worker:optimizer/runs/ primary:optimizer/runs/`
  - `--ignore-existing` guarantees no overwrites of in-progress or completed primary runs.

## To build when green-lit (all NEW files, zero risk to the live gamut)
1. `optimizer/gamut_worker.py` — args: `--plan <path> --reverse [--only-pairs x,y] [--procs N]`. Loop: re-read synced plan + scan runs/ for best_config.json → pick last pending spec → run its cmd (same optimize2_cli invocation, incl. ai_space handling) → repeat. Own STOP file. Writes nothing to plan.json.
2. `scripts/offload_sync.sh` — the two rsync lines + plan copy, run by a `launchd`/cron interval on the worker.
3. `scripts/worker_setup.sh` — one-time: clone repo, `pip install -r requirements`, rsync `optimizer/cache/`, `param_spaces/`, spot/perp candle files from primary.

## Second Mac (same LAN) — steps
1. Enable Remote Login (SSH) on this Mac (System Settings → Sharing). Reachable as `<hostname>.local`.
2. On Mac 2: run worker_setup.sh (clone + env + cache copy; caches are a few GB — one-time over LAN, minutes).
3. Start sync loop + `gamut_worker.py --reverse --procs <Mac2 cores>`.
4. Watch both from the existing panel on the primary (runs appear as they sync in; report.md refreshes as the primary passes skipped specs).
- Expected gain: ~halves wall-clock if Mac 2 is comparable (scale by its core count; rates in HANDOFF are per 14 procs).

## Cloud box — provider comparison (researched 2026-08-02)
NOTE: Hetzner raised CLOUD (CCX/CPX) prices up to ~128% in June 2026 — CCX63 (48 threads) is now €1.37/hr / €853/mo. Cloud VMs are no longer the deal; DEDICATED boxes are.

| Option | Specs | Price | ~20d workload cost | Verdict |
|---|---|---|---|---|
| **Hetzner AX162 dedicated** | 48c/96t EPYC 9454P, 2×1.92TB NVMe | €199/mo + €79 setup | **~€278** (1 month, then cancel) | **BEST DEAL** — 2× the threads of CCX63 at ⅓ the price; ~6 specs concurrently at 14 procs ⇒ 20d → ~3d |
| Hetzner server auction | e.g. Ryzen 5950X 16c/32t boxes | often €50–90/mo, no setup fee, instant | ~€60–90 | Cheapest "second Mac equivalent"; check radar.iodev.org for live deals |
| AWS c7a.16xlarge SPOT | 64 vCPU EPYC Genoa | $3.28/hr on-demand; spot typically 60–90% off (~$1–1.5/hr, fluctuates) | ~$150–250 for ~5 days | Best if avoiding monthly commitment; interruptions are FINE — worker skip-if-done + resumable plan absorb them |
| Hetzner CCX63 cloud | 48 threads | €1.37/hr (€853/mo) | ~€160+ for 5d | Post-hike: dominated by AX162 |
| Contabo big VPS | up to 64 vCPU | very cheap | ? | Shared/oversold cores — real throughput unpredictable; avoid for a deadline |
| OVH Advance 2026 / Netcup RS G12 | ≤16c/32t / ≤24c | mid | mid | Fewer cores than AX162 for similar money |

Recommendation: **AX162 for ~€278 total** (order, run ~3-4 days, cancel within the month) or **AWS spot** if hourly billing is preferred. Verify AX162 availability/provisioning time at order — some configs queue.
- Same worker + sync pattern over ssh (or Tailscale). Box can run 3–4 specs CONCURRENTLY at 14 procs each — needs a `--jobs N` flag on gamut_worker.py (small addition).
- Partition to avoid overlap with Mac 2: give cloud `--only-pairs` for the middle pairs, or let all workers share the same reverse frontier via the sync loop (duplicates stay bounded).
- Teardown: destroy instance when plan.json shows all done; runs are already synced back.

## Caveats / honesty
- Dashboard publish (backtests.js) must be re-run on the primary after syncs for remote backtests to show; runs2/Optimize page picks up synced run dirs as-is.
- Both machines MUST have identical `param_spaces/` and candle caches or results aren't comparable. Setup script enforces by rsyncing from primary.
- Genetic search is stochastic — a run executed on Mac 2 isn't the run the primary would have produced, but each spec executes exactly once, so this is fine.
- AI-space generation (`gen_ai_spaces.py`) needs the variants synced too (covered by param_spaces/ sync); LLM refine needs the key only if regenerating.
- Do NOT run two workers against the same spec name without the sync loop running — skip-if-done is the only lock.

## Decision status
Adrian: planning only for now. Second Mac is on the same home network (rsync over LAN via hostname.local when green-lit).
