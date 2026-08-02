# Story of camp_c4_m_spot_vol3

**What it is:** metax · spot · vol3 · SOL_USDT 3m (perp candles), built 2026-07-21 01:35.

## Components
- ○ mined, unassigned: **camp_c1_w2_merge_prime_spot_trend3** (prime) — holdout 4.0%/mo
    - [2026-07-20 16:47:31] strategy=prime mode=spot method=trend3 algo=genetic total=100000 train-end=2025-09-01 max-hold-days=5 scoring=classic merge-mode=breed
      - COMBINED (breed) from: camp_c1_w1_34_prime_sp_tr3_t, camp_c1_w1_35_prime_sp_tr3_uw
- ○ mined, unassigned: **camp_c1_w1_34_prime_sp_tr3_t** (prime) — holdout 3.4%/mo
    - [2026-07-20 16:22:43] strategy=prime mode=spot method=trend3 algo=genetic total=100000 train-end=2025-09-01 max-hold-days=5 scoring=classic
- ● assigned: **ScalpX_spot_trend3_unified** (scalpx) — holdout 3.2%/mo
    - pre-provenance run (no launch record)
- ○ mined, unassigned: **camp_c1_w1_39_v7_sp_vol3_cl_t** (v7) — holdout 2.6%/mo
    - [2026-07-20 16:34:04] strategy=v7 mode=spot method=vol3 algo=genetic total=100000 train-end=2025-09-01 max-hold-days=5 scoring=classic
- ○ mined, unassigned: **camp_c1_w1_40_sx1_sp_vol3_cl** (scalpx) — holdout 2.4%/mo
    - [2026-07-20 16:36:15] strategy=scalpx mode=spot method=vol3 algo=genetic total=150000 train-end=2025-09-01 max-hold-days=5 scoring=classic
- ○ mined, unassigned: **camp_c1_w1_37_sx2_sp_tr3_uw_t** (scalpx2) — holdout 1.8%/mo
    - [2026-07-20 16:27:24] strategy=scalpx2 mode=spot method=trend3 algo=genetic total=150000 train-end=2025-09-01 max-hold-days=5 scoring=underwater
- ○ mined, unassigned: **spotcamp_A2_macdx_vol3_100k** (macdx) — holdout 0.6%/mo
    - pre-provenance run (no launch record)
- ○ mined, unassigned: **spotcamp_A3_macdx_vol3_500k** (macdx) — holdout 0.6%/mo
    - pre-provenance run (no launch record)

**Honest gate:** walk-forward PASS — chained OOS +7.6%/mo, dd 49%, 14 folds.

**Adopted:** into `config_camp_c4_m_spot_vol3.json` at 2026-07-21 13:05 (dry-run).

**In plain terms** (current LIVE assignment (42d re-assignment cadence)): low-vol: prime (camp_c1_w1_34_prime_sp_tr3_t) · mid-vol: flat · high-vol: v7 (camp_c1_w1_39_v7_sp_vol3_cl_t).

---
*generated 2026-08-01 17:28 (deterministic (no API key)) — regenerate after extending/refining/re-adopting*