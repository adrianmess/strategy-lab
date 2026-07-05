# Research round 2 — multi-strategy, regime methods, window study, max-risk & spot

View everything in **`../../dashboard/index.html`** (open in a browser, or
`cd dashboard && python3 -m http.server` → http://localhost:8000).

## What was run

- **Strategies:** V6 (= V5 family with volatility-normalized (z-scored) MACD
  thresholds, cross-long subsystem re-enabled, optional short trend-block —
  renamed per your instruction since logic was tweaked) and ScalpX (= Scalp
  VRVP+CVD+EMA with regime-adaptive thresholds/branch toggles).
- **Modes:** `lev` — futures, NO stop loss, leverage ≤ 10x, per-trade
  liquidation modeled at (1/lev − 0.8%); constraint: never liquidated +
  mark-to-market drawdown ≤ 80%. `spot` — long-only, 1x, stops ON, 0.05% fees.
- **Regime methods:** none / vol terciles (30d & 7d memory) / volume terciles /
  trend terciles / vol×trend 9-way. All causal (trailing percentiles).
- **Window study:** training window 42d / 91d / 182d / expanding, refit every
  28 days; parameters chosen by multi-core random search (350–1000 candidates
  per refit, top-5 parameter averaging), walk-forward.
- **Honest evaluation:** continuous OOS re-simulation — parameter switches at
  refit dates, positions/equity carry across boundaries, drawdown measured
  mark-to-market (open positions included). Per-fold evaluation overstated
  results badly (it truncated open positions at fold boundaries); several
  configs flipped from "+26%/mo, safe" to "liquidated" under continuous sim.

## Results (continuous OOS, 2024-11-15 → 2026-07-01)

| Config | Result |
|---|---|
| v6 · lev · none · expanding | **125.8× (+28.4%/mo), no liquidation, 64% MTM drawdown** |
| every other lev config (35 of 36) | liquidated |
| v6 · spot · vol3 · expanding | +1.36%/mo (~17%/yr), 19% max DD, 12 stop-outs |
| scalpx · lev (all) | liquidated |
| scalpx · spot (all) | −1.2 to −3.4%/mo (no edge) |
| BASELINE V5 8x stop OFF (your live config) | liquidated 2024-03-14 |

Stress tests on the lev survivor: survives 1-bar signal delay (95×), fees
0.06%/side (91×), fees 0.10%/side (47×). Optimizer's median leverage: 4.5×.

## Files

`wf2.py` (framework: samplers, eval, MTM-DD), `run_wf2.py` (parallel runner),
`resim.py` (continuous OOS re-simulation), `final_search.py` (full-data
production configs), `aggregate_wf2.py`, `build_dashboard.py`,
`scalp_engine.py` (numba Scalp port, intrabar bracket fills),
`wf2_results/` (all 756 fold artifacts), `final_config_*.json` (production
parameter sets).

## Warnings that survive any amount of optimization

1. The 125× survivor is 1 of 36 tested configs — survivorship risk is real,
   and its OOS window contains no March-2024-scale event.
2. No-stop leveraged trading has irreducible ruin risk; the 80% DD cap is a
   backtest constraint, not a guarantee.
3. Compounded multiples assume full-equity reinvestment and exclude funding
   fees; treat monthly growth as the meaningful unit.
4. Do not trade the Scalp strategy.
