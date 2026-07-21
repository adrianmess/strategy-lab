# Campaign c3 — report
updated 2026-07-21 01:04

Ranked by OOS-best holdout %/mo (the honest number). tpm = trades/month; prefer high tpm + modest %/trade (many-small-gains goal). Verify with walk-forward before adopting.

| rank | spec | strat | mode | method | scoring | space | holdout %/mo | dd | tpm | mh(d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | w2_ref_rime_trend3_ts2501_sl | prime | lev | trend3 | classic | levsafe | +42.6% | 37% | 12.4 | 21.0 |
| 2 | s_prime_trend3_ts2501_sl | prime | lev | trend3 | classic | levsafe | +42.3% | 37% | 12.4 | 21.0 |
| 3 | w2_merge_scalpx2_lev_vol3 | scalpx2 | lev | vol3 | classic | default | +34.3% | 85% | 4.8 | 2.2 |
| 4 | w2_ref_x2_vol3_ts2501 | scalpx2 | lev | vol3 | classic | levsafe | +31.7% | 35% | 7.9 | 21.0 |
| 5 | s_sx2_vol3_ts2501 | scalpx2 | lev | vol3 | classic | levsafe | +31.6% | 35% | 7.9 | 21.0 |
| 6 | w2_ref_x2_vol3_ts2509 | scalpx2 | lev | vol3 | classic | levsafe | +18.9% | 66% | 4.3 | 32.1 |
| 7 | s_sx2_vol3_ts2509 | scalpx2 | lev | vol3 | classic | levsafe | +18.6% | 66% | 4.3 | 32.1 |
| 8 | s_macdx_vol3_xfit_lev6 | macdx | lev | vol3 | classic | levsafe6 | +17.8% | 23% | 6.9 | 2.1 |
| 9 | w2_merge_macdx_lev_vol3 | macdx | lev | vol3 | classic | default | +13.3% | 43% | 5.4 | 1.1 |
| 10 | s_macdx_vol3_ts2501 | macdx | lev | vol3 | classic | levsafe | +10.2% | 22% | 7.9 | 3.2 |
| 11 | s_v7_vol3_ts2509_sl | v7 | lev | vol3 | classic | levsafe | +8.6% | 37% | 41.3 | 2.7 |
| 12 | w2_ref_7_vol3_ts2509_sl | v7 | lev | vol3 | classic | levsafe | +8.6% | 37% | 41.3 | 2.7 |
| 13 | s_macdx_vol3_xfit | macdx | lev | vol3 | classic | levsafe | +8.3% | 9% | 1.3 | 0.3 |
| 14 | w2_ref_acdx_vol3_ts2501 | macdx | lev | vol3 | classic | levsafe | +8.1% | 26% | 7.7 | 4.5 |
| 15 | s_v6_vol3_alt21_sl | v6 | lev | vol3 | classic | levsafe | +6.8% | 51% | 3.3 | 6.0 |
| 16 | w2_ref_acdx_vol3_xfit_lev6 | macdx | lev | vol3 | classic | levsafe6 | +3.7% | 73% | 9.5 | 2.9 |
| 17 | s_v7_vol3_alt21rec_sl | v7 | lev | vol3 | recent | levsafe | +0.2% | 43% | 21.7 | 2.3 |
| 18 | s_v7_vol3_alt21_sl | v7 | lev | vol3 | classic | levsafe | -0.6% | 54% | 25.6 | 1.9 |
| 19 | s_v7_vol3_xfit_sl | v7 | lev | vol3 | classic | levsafe | -1.8% | 22% | 9.9 | 4.3 |
| 20 | s_rocx_trend3_alt21_r | rocx | lev | trend3 | classic | levsafe_ratchet | -2.9% | 43% | 9.7 | 0.3 |
| 21 | s_macdx_trend3_alt21 | macdx | lev | trend3 | classic | levsafe | -3.1% | 62% | 8.6 | 1.8 |
| 22 | w2_merge_v7_lev_vol3 | v7 | lev | vol3 | classic | default | -5.8% | 42% | 17.3 | 2.6 |
| 23 | s_v7_vol3_ts2501_sl | v7 | lev | vol3 | classic | levsafe | -10.4% | 59% | 32.4 | 2.3 |
| 24 | s_rocx_trend3_ts2501_r | rocx | lev | trend3 | classic | levsafe_ratchet | -10.7% | 72% | 14.8 | 4.0 |
| 25 | s_macdx_vol3_alt21 | macdx | lev | vol3 | classic | levsafe | -16.5% | 94% | 8.7 | 1.9 |
| 26 | s_prime_trend3_alt21_sl | prime | lev | trend3 | classic | levsafe | -18.4% | 98% | 3.6 | 8.2 |
| 27 | s_v7_vol3_ts2509_sl_lev6 | v7 | lev | vol3 | classic | levsafe6 | -44.5% | 87% | 25.5 | 5.0 |

No survivors / negative holdout: s_sx2_vol3_alt21