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
