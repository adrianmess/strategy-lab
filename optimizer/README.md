# Optimizer — reusable, multi-process, long-running

The optimization method from the research rounds, packaged so you can run it
yourself: pick a strategy/mode/regime-method, choose how many CPU processes
and how long to run, and it searches until the budget is spent. Everything is
resumable and publishes to the dashboard.

## The three tools

### 1. `optimize_cli.py` — parameter search on full history
```bash
# all combinations of flags work; --procs = number of processes (your choice)
python3 optimize_cli.py --strategy v6 --mode lev --method none \
    --procs 8 --hours 6 --name overnight_lev

python3 optimize_cli.py --strategy v6 --mode spot --method vol3 \
    --procs 8 --total 100000 --name big_spot     # candidate-count budget instead
```
- Re-run with the same `--name` to continue where it stopped (pool is saved
  every batch). Run it for days if you want.
- Output: `runs/<name>/best_config.json` (top-5 parameter-averaged candidate).

### 2. `walkforward_cli.py` — the honest evaluation
```bash
python3 walkforward_cli.py --strategy v6 --mode lev --method none \
    --window all --refit-days 28 --samples 500 --procs 8 --name wf_check
```
Re-optimizes at every refit date using only past data, tests on the next
unseen window, then runs the **continuous re-simulation** (positions carry
across refit boundaries, drawdown is mark-to-market). This number is the one
to believe — full-history fits from `optimize_cli` are in-sample.

### 3. `backtest_cli.py` — publish to the dashboard Backtests page
```bash
python3 backtest_cli.py --config runs/overnight_lev/best_config.json --name overnight_lev
python3 backtest_cli.py --walkforward runs/wf_check --name wf_check
```
Adds an entry (stats, MTM equity curve, monthly returns, trade list) to
`dashboard/backtests.html`.

## Knobs shared by both search tools

| flag | meaning |
|---|---|
| `--strategy` | `v6` (V5-family, volatility-normalized) or `scalpx` (Scalp-family) |
| `--mode` | `lev` (futures, no stop, lev ≤ 10, liquidation modeled, MTM-DD ≤ 80%) or `spot` (long-only 1x, stops on, DD ≤ 50%) |
| `--method` | regime classifier: `none`, `vol3`, `vol3_7d`, `volume3`, `trend3`, `volXtrend9` |
| `--procs` | worker processes — set to your core count (or cores−1) |

## Refreshing data first

```bash
python3 ../adaptive_trader/research/update_data.py   # extends CoinAPI history to now
```
(clears derived caches; the next optimizer run rebuilds them, ~1 min)

## Adding a new strategy

1. Write an engine like `research2/scalp_engine.py`: a `precompute(df3)` that
   turns bars into indicator arrays, and a numba `run_*` loop that takes a
   per-bar `regime` array and a per-regime parameter matrix `P`, returning a
   trade array `[entry_idx, exit_idx, dir, ..., net, mae, reason, lev]`.
2. In `research2/wf2.py` add: a sampler `sample_<name>(rng, R, mode)` (dict of
   per-regime parameter lists), a builder `build_P_<name>(cand, R)`, and a
   branch in `eval_config` + `load_globals` that runs your engine.
3. Everything else (walk-forward, resim, dashboards, refit) works unchanged.

## Notes

- Search is random sampling + top-5 parameter averaging. Simple, parallel,
  and hard to fool; with long budgets it explores widely. Scores use
  mean-minus-half-std of monthly log growth, so lucky one-month wonders rank low.
- Feasibility gates (liquidation, drawdown caps, min trades) live in
  `wf2.feasible()` — edit there if you want different risk rules.
- Backtests exclude funding fees; fills are next-bar-open (entries) and
  intrabar (scalp brackets). CoinAPI history has a hole Jun–Nov 2024.
