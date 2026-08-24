# HARVX — harvesting chop inside red windows (research log, 2026-08-25)

## Origin

Adrian traded MEX2 Spot manually while the XRP component's virtual trade sat
~4% underwater (virtual entry 1.5437, 2026-08-22 08:09): force-closed BTC
(+0.37%), adopted XRP low, sold strength (+1.97%), re-entered lower, repeated.
Net ≈ **+2.9% while mirror-following would have been −4.2%**. Question: can
this be automated, and with what — fixed rules, indicators, or something
dynamic?

The state being exploited is specific: **a long thesis ≥1% underwater with
price chopping** ("red window"). Definition used everywhere below: rolling
3-day windows sampled every 6h; a window joins the set at the first 1m close
≥1% under its start.

## Evidence chain (each step killed an idea or confirmed one)

1. **His exact window, fixed tp/dip cycling** (take profit +p%, re-enter at
   −d% below exit, long-only spot, 0.05% taker/side): every combo profitable,
   best **+4.2% net / 7 cycles (tp 0.75 / dip 0.75)** vs −0.5% adopt-and-hold.
   → the loop automates, but one window proves nothing.

2. **905 red windows, 9 months, XRP**: hold-to-end −485% cumulative; fixed
   cycling −280..−295%. Improvement is real (~+0.2%/window) but red windows
   stay net losers. → improvement overlay, not a money machine.

3. **RSI gating** (enter only oversold ~70min-RSI<32, exit >72): XRP loss cut
   to **−58%** (8× better than fixed). → indicator gates matter a lot.

4. **HYPE flips the sign**: red windows there RECOVERED (+830% hold);
   almost every harvest variant underperformed holding — fixed take-profits
   amputate violent rebounds. → **no universal rule; per-pair training
   required.**

5. **Textbook scalping battery** on 5m with dynamic ATR-trailing stops and
   moving targets — Bollinger reversion, VWAP-deviation reversion, Connors
   RSI(2), Keltner+stochastic, supertrend: **all lost badly on both pairs**
   (e.g. XRP: hold −506% vs bbrev −1,515%, rsi2 −3,315%). Three causes:
   - trailing stops are poison in mean-reversion chop (they realize every dip)
   - 13–41 trades/window × 0.1% round-trip = fee death
   - method choice cannot overcome per-asset regime differences
   → what works: patient oversold entries, exits into strength at MOVING
   levels, **no stop-loss** (spot; the runner's liq-guard covers disaster),
   few cycles, maker orders.

## The trained policy (harvx)

`optimizer/harvx_train.py` — numba sim + genetic search, ~40B bar-iters/min
on 12 cores (35 runs × 100k evals ≈ 35 min total). Genome (all distances
scale with realized hourly vol — nothing is a fixed percent):

hold_mode (do-nothing arm) · RSI window 30/70/140m · rsi_lo gate · rsi_hi
exit · re-entry dip ×vol · exit kind (tp / mean-or-overbought / overbought) ·
tp ×vol · exit-mean 1h/4h · max cycles · cooldown. No stop-loss by design.

Holdout schemes per pair: hA (holdout = recent tail), hL21 (alternating 21-day
blocks), hB (holdout = oldest year), hM (holdout = middle band), hN (train
only). Judged like the main pipeline: top-20 train genomes get one holdout
verdict each.

## Results (2026-08-25, data 2024-01→2026-08, taker fees)

| pair | holdouts positive | best holdout | notes |
|------|------------------|--------------|-------|
| HYPE | 3/4 | **+13.1 over 872 windows, 61% wins, worst −0.18%** (hL21) | best pair; optimizer chose ACTIVE cycling (RSI30<40, exit ob>72, tp 2.3×vol, ≤10 cycles) — vol-scaled tp keeps rebound upside |
| SOL  | 3/4 | +3.8 (hL21), +3.2 (hB) | real, modest |
| ETH  | 3/4 | +1.5 (hL21) | real, small |
| BTC  | 2/4 | +0.23 | marginal |
| SUI  | 2/4 | +0.72 | marginal |
| XRP  | 2/4 | +0.84 | weak — needs extreme gates (RSI<14); the good manual week was a favorable stretch, not a persistent edge |
| DOGE | 0/4 | ~0 | best policy ≈ don't trade |

Winning genomes share: oversold gates, vol-scaled distances, exits into
strength at moving levels, no SL, low cycle counts, cooldowns.

**Caveats**: windows overlap 12× (totals overstate independent P&L — trust
the win-rates and worst-window figures); taker fees assumed (maker = 0% on
MEXC spot improves everything); overlapping-window months make the
neg-month penalty indicative, not gospel.

## Artifacts

- Trainer: `optimizer/harvx_train.py` (`--all --total 100000 --procs 12`)
- Results + genomes: `optimizer/harvx/<pair>_<scheme>.json`
- Research sims: `optimizer/harvest_hist_sim.py`, `optimizer/scalp_regime_lab.py`
- Full run log: `optimizer/harvx/train_run.log`

## Next steps (not started)

1. Runner "harvest mode": execute the trained per-pair policy ONLY in
   adopted/late-joined red states, spot only, maker limit orders, cycle caps
   + liq-guard intact. Dry-run first; live only via confirm. Candidate pairs:
   HYPE, SOL, ETH. Skip DOGE/XRP/BTC/SUI on current evidence.
2. Re-run trainer with maker fees to size the execution edge.
3. Non-overlapping window validation (sample every 3 days) to de-bias totals.
