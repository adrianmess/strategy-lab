# Performance improvement options (Mac mini CPU spikes)

Written 2026-08-27 after diagnosing the mini's every-few-seconds 100% CPU
spikes. Reference for future work — none of the "proposed" items below are
implemented; do them in order of ROI when performance matters again.

## Context: where the CPU goes today

The FCFS traders run ~20 `fcfs_host.py` processes (12 spot + 8 lev). Each
host, on every closed bar of its timeframe, re-runs its components' research
engines over the FULL evaluation window (~44,000 bars) — the "engine-exact"
design: live behavior equals the backtest because live literally re-executes
the backtest each bar. All 1m bars close at the same wall-clock instant, so
~14 hosts burst simultaneously for 2–5s right after each minute boundary.
That synchronized burst IS the spike. It is a design cost, not a bug.

The bar-aligned polling + WebSocket wake-up added 2026-08-27 (signal latency
13–16s → ~5–8s) deliberately concentrates evaluations at the boundary, which
made the spike slightly sharper. The remaining latency floor is the engine
evaluation itself (2–5s) + 1.5s arbitration + order round-trip.

## Resolved already

- **slbench leftover** (removed 2026-08-27): an orphaned launchd job with
  keepalive re-ran `/tmp/mapbench3.py` (10-proc optimizer benchmark) forever
  — 31k runs. Was the dominant spike source and drove `fseventsd` to ~100%.
  Lesson: `launchctl submit` + keepalive outlives the session that created
  it; clean up benchmarks.

## Proposed, cheap → expensive

### 1. Spotlight exclusion (zero risk, approved-but-not-yet-done as of writing)

`mds`/`mds_stores`/`mdworker` re-index the machine-generated churn in the
repo forever. Exclude on the mini (and optionally the MacBook):

    sudo mdutil -i off /Users/admn/strategy-lab/optimizer/cache
    sudo mdutil -i off /Users/admn/strategy-lab/optimizer/runs

No functional impact — nobody Spotlight-searches numba caches or run dirs.

### 2. Trim the evaluation window (likely 3–4x spike reduction, needs validation)

Hosts replay ~44k bars per component per bar; the engine's END state
(current signal + open virtual trade) is overwhelmingly determined by
warmup (3000 bars) + recent history bounded by max-hold and cooldown spans.
A ~12k-bar window should be equivalent in practice at ~1/4 the cost.

Caveat: trade sequences are path-dependent (cooldowns, entry gating), so a
shorter window is NOT bit-identical — though the 44k window is itself
already an approximation of the full-history backtests. REQUIRED before
going live: an offline parity run on the MacBook — evaluate a week of
historical bars with both window sizes across all components and require
agreement of the emitted signals/shadow states. If they agree, change
the window in `fcfs_host.py` / `Feed.backfill` and ship.

Also worth measuring first: how the 2–5s splits between `compute_features`
and the engine replay — if features dominate, caching them between bars is
a separate cheap win.

### 3. Flatten the burst: stagger or concurrency cap (safe, costs a little latency)

- Stagger: hosts evaluate at boundary + (host_index % k) * 0.5–1s. Spreads
  the peak into a ramp; later hosts' signals arrive up to ~3s later.
- Concurrency cap: shared semaphore (file-lock) limiting simultaneous
  evaluations to ~6; halves the peak, second wave lands +2–4s.

Both are semantics-preserving (same decisions, slightly later). They hand
back a couple of the seconds the 2026-08-27 latency work bought.

### 4. Incremental ("continuous") evaluation — the full solution, weeks not days

Advance persistent engine state one bar per bar instead of replaying the
window: spikes vanish AND latency floor drops (the 2–5s eval → microseconds).
This is what TradingView's Pine runtime does — but Pine guarantees it by
constraining the language to stateful primitives; our engines are arbitrary
numba kernels, so it's a retrofit.

Why "weeks": the typing is ~3–5 days (three engine cores' loop-carried state
made explicit + incremental features — EMAs trivial; rolling z-scores,
regime quantiles, and the scalp POC volume profile are real data structures).
The calendar is dominated by:
- float-exactness debugging: incremental summation lands ULPs away from the
  vectorized batch math; a 1e-13 difference flips a threshold crossing on
  the wrong bar and cascades into a permanently different trade sequence.
  Find-first-divergence/fix/rerun loops are unpredictable time sinks.
- an offline parity harness (old vs new over months of bars, all comps,
  agreement required on every signal and shadow state).
- an irreducible 1–2 week LIVE shadow period: run incremental beside full
  replay on the production hosts, alert on any disagreement, before it may
  drive orders. Production-only events (feed hiccups, 510 storms, restarts
  mid-bar) can't be simulated offline.
- checkpoint/restart handling + periodic full-replay resync to repair drift.

Do this only if going sub-minute timeframes or if (2)+(3) prove
insufficient. If built: define constrained stateful primitives (Pine-style)
rather than hand-porting each kernel.

### Ruled out

- **De-prioritizing hosts (nice/QoS)**: macOS pushes background-QoS work to
  efficiency cores with a measured ~25x penalty (see panel_watchdog.sh notes,
  2026-08-24). Turns a 3s spike into a 30s smear and degrades signal latency.

## Recommended sequence

1. Spotlight exclusion (minutes).
2. Measure: features-vs-engine profile + window-trim parity test on the
   MacBook (no live impact).
3. If parity holds → trim window (biggest cheap win).
4. Stagger/cap only if the flattening is still wanted afterwards.
5. Option 4 only with a strong reason.
