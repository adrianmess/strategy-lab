# Autonomous V7 Campaign — no-liquidation, 2026-active leveraged configs

**Mission** (Adrian, 2026-07-09): find ≥2 leveraged `V5 family · V7 full-param` configs that
(1) never liquidate — holdout AND full backtest — and (2) actively open trades through 2026,
where nearly all previous winners went dormant. Anchored to strategy live defaults, 14 processes.

**Success criteria used**: full backtest no-LIQ · holdout/OOS verdict no-LIQ ·
≥15 trades in 2026 spread over ≥4 distinct months (specifically activity after Feb 2026,
the stretch where prior winners were stuck or silent).

---

## Baseline review (before any new runs)

Scanned every published leveraged backtest for 2026 activity:

- **Almost everything stops trading 2026-01-14..16.** The +46%/mo family, the +18.6% pair,
  all of them — one long entered mid-January 2026 near $130–145 sits open for months
  (PT-only exits), freezing the single position slot. Same root cause diagnosed earlier.
- **The exception: max-hold runs.** `opt2_0708_1137_HOLDOUT` and `opt2_0708_1039_HOLDOUT`
  (≤14d max-hold) traded **30 times across all 7 months of 2026**, +6.4%/mo, no liq.
  The max-hold disqualifier forces capital recycling → 2026 activity. Their weakness:
  full-history backtests liquidated (in-sample, pre-holdout period at ~6–8× leverage).
- Prime originals (defaults) trade sparsely into 2026 (7–8 trades) — the quick-exit
  character works but entry filters go quiet in the bear grind.
- Reservoir lesson (from the 1M-eval runs): at big budgets, ALL holdout survivors come
  from the uniform reservoir (train-ranks #331–#574); the elite top-300 is pure overfit.

**Hypotheses for the campaign**:
- H1: max-hold (7–10d) is necessary for 2026 activity.
- H2: alternating holdout (vs date-split) lets candidates *train* on 2026-like blocks →
  more likely to trade them; date-split candidates never see 2026 at all.
- H3: trend3 regimes (direction-aware) should beat vol3 in the 2026 bear grind
  (down-regime can enable shorts / disable dip-longs).
- H4: tighter max-DD (60%) + liq-margin gate should fix the full-history liquidation
  problem that killed the 1137-style configs.
- H5: underwater scoring reinforces the quick-recycling character.

## Run matrix — wave 1 (all: v7 · lev · 14 procs · anchored defaults λ0.3 ·
underwater scoring · skip_contaminated · ~500k evals)

| run | regime | holdout | max-hold | max-DD | rationale |
|---|---|---|---|---|---|
| auto_v7_A_vol3_alt30_mh10 | vol3 | alt 30d | 10d | 60% | H1+H2+H4: the 1137 recipe, trained on 2026 blocks, tighter DD |
| auto_v7_B_trend3_alt21_mh7 | trend3 | alt 21d | 7d | 60% | H3: direction-aware regimes for the bear grind |
| auto_v7_C_crossfit_lockbox26 | vol3 | cross-fit fold 30d | 10d | 60% | lockbox 2026-02-01.. — judged once on exactly the months others fail |

Results appended below as they complete.

---
## Wave 1 results

**auto_v7_A_vol3_alt30_mh10** — FAIL (instructive). Holdout survivor (+13.2%/mo, dd 24%,
reservoir rank #491) but full backtest LIQUIDATED in early 2024, 0 trades in 2026, with a
short from Jan 2024 riding to −107% unrealized. **Root cause found: alternating-block
evaluation caps observable hold time at the block edge (positions are dropped there,
uncounted), so the ≤10d max-hold gate passes inside blocks while continuous reality holds
for months. Blocked training structurally hides hold-forever behavior.** H2 (alternating
teaches 2026) is outweighed by this loophole for the no-liq goal.

**Revised plan (wave 2)**: date-split training (train-end 2025-09-01) — training is then
CONTINUOUS, so max-hold and liquidation gates bind for real. 2026 activity should still
follow from the max-hold constraint alone (the 1137 baseline traded all of 2026 despite
never training on it). The old 1137 full-backtest liquidation predated the search/backtest
gap-consistency fix, so the recipe deserves a clean rerun.

| run | regime | holdout | max-hold | max-DD |
|---|---|---|---|---|
| auto_v7_D_vol3_te0901_mh7 | vol3 | date 2025-09-01 | 7d | 60% |
| auto_v7_E_trend3_te0901_mh14 | trend3 | date 2025-09-01 | 14d | 60% |

**auto_v7_B_trend3_alt21_mh7** — PARTIAL. Train-best liquidates on full history (same
blocked-eval loophole as A), but the OOS-best survives the continuous full backtest
(+2.0%/mo, no liq) AND trades in 2026 (Feb/Mar/Jun — 3 trades). trend3 direction-awareness
shows signs of life in the bear grind, but activity is too thin to qualify (need ≥15/≥4mo).

**auto_v7_C_crossfit_lockbox26** — FAIL. Cross-fit found 480+337 cross-fold survivors and a
300-strong merge pool, but ALL 15 finalists LIQUIDATED on the 2026-02.. lockbox; full
backtest LIQ, 0 trades in 2026. Confirms wave-1 conclusion: any *blocked* evaluation
(alternating folds) breeds hold-dependent configs that die continuously. Cross-fit remains
the right honesty harness, but for the no-liq mission its folds would need to be continuous
sub-periods rather than interleaved blocks.

## Wave 2 results

**auto_v7_D_vol3_te0901_mh7** — ✅ **QUALIFIER #1** (its OOS-best config).
`auto_v7_D_vol3_te0901_mh7_oosbest_full`: NO liquidation on full continuous history,
+13.9%/mo, **36 trades across all 7 months of 2026**, no open position at the end.
Continuous date-split training made the ≤7d max-hold and liquidation gates bind for real;
the max-hold character then keeps trading straight through the 2026 bear grind it never
trained on. Caveats: 84% MTM DD over full history (train cap was 60% — the excess is
pre-train-period at market extremes) and worst excursion −16.3%; margins are thin.
Note: only 11 candidates were ever feasible in 500k evals — the constraint set is brutal,
which is precisely why what survives generalizes.

**auto_v7_E_trend3_te0901_mh14** — FAIL. Both best and OOS-best liquidate on full history.
trend3 at 14d holds didn't find a continuous survivor; the 7d/vol3 recipe remains the one
that works.

**auto_v7_D_survivor2_full** — ✅ **QUALIFIER #2**. Run D's strict rank-#9 survivor
(≤7d holds verified on holdout), backtested over full continuous history:
NO liquidation, +5.0%/mo, MTM DD only 32%, worst excursion −6.3% (clears the liquidation
line at any leverage up to ~14×), 28 trades across Jan–Jun 2026, nothing open at the end.
Lower returns than qualifier #1 but far stronger risk profile.

---

## FINAL RESULT — qualifying runs (criteria: leveraged, no liquidation on full
continuous backtest, actively trading through 2026)

1. **auto_v7_D_vol3_te0901_mh7_oosbest_full** — +13.9%/mo, 36 trades in all 7 months of
   2026, no liq, no stuck position. Higher octane; caveats: 84% full-history MTM DD,
   worst excursion −16.3% (thin margin), and its holdout hold-time slightly exceeded the
   7d target (selection bug, fixed in code afterwards).
2. **auto_v7_D_survivor2_full** — +5.0%/mo, 28 trades across Jan–Jun 2026, no liq,
   32% DD, worst excursion −6.3%. The robust pick.

Both come from run **auto_v7_D_vol3_te0901_mh7** (v7 · lev · vol3 · train-end 2025-09-01 ·
max-hold 7d · max-DD 60% · underwater scoring · anchored to live defaults λ0.3 · 500k evals).

# SPOT Campaign — day-trade/scalp, long-only 1x (Adrian, 2026-07-12)

**Mission**: spot-mode configs that day-trade/scalp toward ~1%/day equivalent.
Tiers: STRETCH ≥~35%/mo · STRONG ≥10%/mo · MEANINGFUL ≥3%/mo and beats buy-and-hold
with lower DD. 14 procs; budget ladder 10k → 100k → 500k → 1M as results warrant.

**Benchmarks (buy-and-hold SOL over our data)**: −0.9%/mo overall; −2.6%/mo
(Nov-24..Nov-25) and −9.6%/mo (Nov-25..Jul-26) in the last 19 months; holdout
window (after 2025-09-01) is deep bear. ANY positive holdout %/mo is real alpha.
Spot economics: 0.10% round-trip fees → a 0.4% scalp keeps ~75% of gross;
long-only → must be OUT of the market most of the bear tape (trend3's down-regime
can disable longs — key lever).

**Context**: spot spaces are now separate (@spot, wide exits after the pinned-walls
diagnosis) and the spot leverage/shorts mutation leak is fixed — all pre-fix flat
spot results were invalid. Clean prior art: __macdx_spotspace_test OOS-best
+2.1%/mo full-history, no liq (swing character, PT 8-11%).

## Wave 1 — scouting at 10k evals (all: spot · 14 procs · train-end 2025-09-01 ·
genetic · classic scoring · skip_contaminated · wide @spot spaces)

| run | strategy | regime | rationale |
|---|---|---|---|
| spotcamp_A_macdx_vol3_10k | macdx | vol3 | crossover system, volatility-keyed params |
| spotcamp_B_macdx_trend3_10k | macdx | trend3 | H: long-only needs down-regime long-blocking |
| spotcamp_C_scalpx2_vol3_10k | scalpx2 | vol3 | scalp family, full-param variants |
| spotcamp_D_scalpx2_trend3_10k | scalpx2 | trend3 | scalp + direction awareness |
| spotcamp_E_prime_trend3_10k | prime | trend3 | dip system + down-regime blocking |

### Wave 1 results (10k each; holdout = after 2025-09-01, deep bear, B&H ≈ −13%/mo)

- **E prime·trend3 — WINNER.** Only positive holdout: OOS-best +0.8%/mo (dd 21%,
  tpm 15.4, in-market 29%). Full backtest +6.6%/mo, ×4.8, no liq. Day-trade cadence,
  mostly OUT of the market — the trend3 long-blocking hypothesis works on spot.
- A macdx·vol3 — full +3.2–3.6%/mo but holdout −1.3%/mo (in-sample flattered). Keep.
- B macdx·trend3 — holdout −2.4, full +2.1. C scalpx2·vol3 — worst (holdout −7.5,
  dd 58%). D scalpx2·trend3 — holdout −2.3, full +1.9. Scalp family struggles with
  spot fees; prime's dip-buy + trend gate fits the tape better.

**Wave 2**: scale E and A to 100k evals, same recipes (budget is the only change).

### Wave 2 results (100k) + the spot pool-scan fix

Raw 100k finalize looked WORSE (both holdouts negative) — cause found: the finalize
scan's early-stop ("stop after 10 non-liquidated") degenerates on spot, where nothing
can liquidate, to "scan only the top-10 in-sample". FIXED: spot scans the whole
pool + reservoir. Re-finalized both runs; true OOS-bests were deep in the reservoir
(train-ranks #525 and #506), consistent with the lev-campaign reservoir doctrine.

- **E2 prime·trend3 100k, OOS-best #525**: holdout +3.3%/mo dd 0%; full backtest
  +3.6%/mo, ×2.41. Clean 1x long-only.
- **A2 macdx·vol3 100k, OOS-best #506**: holdout +0.6%/mo dd 20%; full backtest
  +5.2%/mo, ×3.49.
Both beat B&H (−0.9%/mo overall, −13%/mo holdout window) with far lower DD ->
MEANINGFUL tier cleared. E-anatomy (10k OOS-best): 98% win rate, +0.72% avg win,
median hold 1.2h, but 8 stop-losses averaging −11.3% = all the bleed months.

**Wave 3**: 500k on both recipes; plus two tail-cutting variants at 100k on E
(underwater scoring · max-hold 2d) aimed at the −11% stop-riders.

### Wave 3 results — plateau found, hypotheses settled

- **500k adds nothing over 100k** (E3 OOS +2.9%/mo ≈ E2's +3.3; A3 OOS-best =
  literally A2's pick). 100k evals is the budget sweet spot for spot searches.
- **Tail-cutting REJECTED both ways**: underwater scoring (E4) halved the holdout
  (+1.7 vs +3.3); max-hold 2d (E5) killed the edge (−0.3). The rare −11% stops are
  the price of the 98%-win dip-buying character, not removable inefficiency.

### Addendum — V4 ROC/SMA (rocx) ported and campaigned (2026-07-12)

New strategy ported from Adrian's pine: long-only, entries only on new 45-min
buckets when sampled 1-min ROC(low) falls toward the present AND the 45-min SMA
slope is up; TP vs signal close; pine-exact "trailing" stop that FOLLOWS the
close both directions (fires only on a single-close 2% drop). Engine validated
11/13 vs the TV export (1 sub-tick feed diff, 1 beyond-data exit); wired in as
rocx_original (quick backtest) + rocx (optimizer, per-regime, ROC/SMA length
variant menus, spot/lev spaces).

Campaign (spot, train-end 2025-09-01, full pool scans):
- defaults (pine values): −2.3%/mo full history, ×0.56 — loses.
- R1 vol3 10k: OOS-best −0.4%/mo holdout; R2 trend3 10k: −0.8.
- R3 vol3 100k / R4 trend3 100k: NO positive-holdout candidate in either full
  600-scan (best −2.6%/mo). More budget made it worse (deeper in-sample fit).
**Verdict: rocx has no spot edge in the bear holdout — its dip-buy needs the
45-min uptrend that the holdout doesn't have. Does NOT displace E2/A2.**
Full-history OOS-config numbers (+1.6..+4.1%/mo) are in-sample-era earnings;
holdout says don't trust them.

## SPOT CAMPAIGN — FINAL RESULT

Winners (both clean 1x long-only, positive holdout in a −13%/mo B&H window):
1. **spotcamp_A2_macdx_vol3_100k_oosbest_full** — +5.2%/mo full history (×3.5),
   holdout +0.6%/mo dd 20%. Character: 63% win, symmetric ±3.9%, ~11h holds,
   PT 6–10%, SL ~4.5%. 6 neg months / 23.
2. **spotcamp_E2_prime_trend3_100k_oosbest_full** — +3.6%/mo full (×2.4), holdout
   **+3.3%/mo dd 0%**. Character: 98% win, +0.72% avg, 1.2h median holds,
   in-market 29%; bleed = 8 stops averaging −11%.
3. **50/50 blend** (monthly corr 0.16): **+4.5%/mo, worst month −4.0%, 4 neg
   months / 23** — the drawdown halves while keeping most of the return.

vs mission tiers: MEANINGFUL cleared decisively (beat B&H by ~5-6%/mo with a
fraction of the DD); STRONG (≥10%/mo) not reached; STRETCH 1%/day (~35%/mo) is
not achievable on 1x long-only spot with these engines — best observed ≈0.17%/day
equivalent. The honest lever for more: leverage (lev mode already delivers this),
or new entry signals, not more optimization budget.

Machinery fixed during the campaign: spot mutation leverage/shorts leak;
spot pool-scan early-stop (now scans all pool+reservoir on spot).
Next candidates (not run): walk-forward validation of A2/E2 before any live use;
portfolio backtest feature for the blend.

---

## Why this recipe worked (and the others didn't)

- **Continuous training is non-negotiable for no-liq.** Every blocked evaluation
  (alternating folds, cross-fit folds) breeds configs that depend on positions being
  dropped at block edges; they liquidate the moment reality runs continuously (A, B, C, E's
  train-bests all died this way).
- **The ≤7d max-hold disqualifier is the 2026-activity lever.** It doesn't teach the
  strategy 2026 — it forbids the stuck-position behavior that froze every previous winner
  on Jan 16, 2026. Capital stays free, so normal entry signals (which kept firing all along)
  get taken.
- **Underwater scoring + anchor keep the character sane**; the anchor seeds the viable
  region, the scoring penalizes capital lock-up.
- **Feasibility scarcity is a feature**: only 11 of 500k candidates were feasible under the
  full constraint set; both qualifiers came from that tiny set and both generalized.
  Contrast: looser runs produce 300-deep pools that are 100% overfit.
- Next ideas if more candidates are wanted: rerun D's recipe with a bigger budget (more of
  those rare feasibles), mh10 variant, or max-DD 50% to pre-tame the full-history DD.
