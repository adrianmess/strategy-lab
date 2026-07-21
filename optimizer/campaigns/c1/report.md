# Campaign c1 — report
updated 2026-07-20 20:14

Ranked by OOS-best holdout %/mo (the honest number). tpm = trades/month; prefer high tpm + modest %/trade (many-small-gains goal). Verify with walk-forward before adopting.

| rank | spec | strat | mode | method | scoring | space | holdout %/mo | dd | tpm | mh(d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | w1_01_v7_vol3_uw_d | v7 | lev | vol3 | underwater | default | +22.7% | 76% | 5.8 | 3.0 |
| 2 | w1_02_v7_vol3_cl_d | v7 | lev | vol3 | classic | default | +17.5% | 58% | 6.1 | 2.9 |
| 3 | w1_12_v7_vol3_ww_t | v7 | lev | vol3 | worst_window | tight | +14.1% | 49% | 2.5 | 2.6 |
| 4 | w1_27_macdx_vol3_cl_d | macdx | lev | vol3 | classic | default | +13.1% | 85% | 6.2 | 4.9 |
| 5 | w1_09_v7_volume3_cl_d | v7 | lev | volume3 | classic | default | +11.4% | 30% | 4.1 | 2.5 |
| 6 | w1_18_prime7_trend3_cl | prime7 | lev | trend3 | classic | default | +11.2% | 59% | 4.0 | 4.7 |
| 7 | w2_ref_27_macdx_vol3_cl_d | macdx | lev | vol3 | classic | default | +10.9% | 84% | 6.3 | 3.7 |
| 8 | w1_22_sx2_vol3_cl_d | scalpx2 | lev | vol3 | classic | default | +10.3% | 67% | 4.4 | 2.3 |
| 9 | w2_merge_scalpx2_lev_vol3 | scalpx2 | lev | vol3 | classic | default | +7.5% | 28% | 2.4 | 0.8 |
| 10 | w1_33_v6_vol3_cl_d | v6 | lev | vol3 | classic | default | +6.7% | 39% | 2.5 | 2.4 |
| 11 | w2_merge_v7_lev_vol3 | v7 | lev | vol3 | classic | default | +6.1% | 33% | 4.1 | 2.2 |
| 12 | w1_20_prime_trend3_cl | prime | lev | trend3 | classic | default | +4.7% | 63% | 1.8 | 2.8 |
| 13 | w1_15_v7_vol3_uw_dd35 | v7 | lev | vol3 | underwater | default | +4.7% | 83% | 2.6 | 2.9 |
| 14 | w1_11_v7_vol3_ww_d | v7 | lev | vol3 | worst_window | default | +4.2% | 16% | 4.2 | 1.3 |
| 15 | w2_merge_prime_spot_trend3 | prime | spot | trend3 | classic | default | +4.0% | 0% | 4.8 | 4.7 |
| 16 | w1_31_rocx_vol3_cl_r | rocx | lev | vol3 | classic | ratchet_both | +3.8% | 86% | 7.4 | 4.9 |
| 17 | w1_25_sx2_vol3_uw_mh05 | scalpx2 | lev | vol3 | underwater | default | +3.5% | 13% | 1.8 | 0.2 |
| 18 | w1_34_prime_sp_tr3_t | prime | spot | trend3 | classic | tight | +3.4% | 0% | 4.1 | 2.8 |
| 19 | w1_23_sx2_trend3_uw_d | scalpx2 | lev | trend3 | underwater | default | +2.6% | 14% | 4.5 | 4.1 |
| 20 | w1_39_v7_sp_vol3_cl_t | v7 | spot | vol3 | classic | tight | +2.5% | 13% | 8.9 | 4.8 |
| 21 | w1_17_prime7_vol3_cl | prime7 | lev | vol3 | classic | default | +2.4% | 18% | 2.4 | 1.0 |
| 22 | w1_40_sx1_sp_vol3_cl | scalpx | spot | vol3 | classic | default | +2.4% | 12% | 10.9 | 4.4 |
| 23 | w1_37_sx2_sp_tr3_uw_t | scalpx2 | spot | trend3 | underwater | tight | +1.8% | 11% | 3.4 | 2.7 |
| 24 | w1_35_prime_sp_tr3_uw | prime | spot | trend3 | underwater | default | +0.7% | 16% | 3.8 | 2.8 |
| 25 | w1_36_macdx_sp_vol3_t | macdx | spot | vol3 | classic | tight | +0.3% | 11% | 2.6 | 3.4 |
| 26 | w1_21_sx2_vol3_uw_d | scalpx2 | lev | vol3 | underwater | default | +0.3% | 11% | 1.4 | 2.4 |
| 27 | w1_24_sx2_vol3_uw_t | scalpx2 | lev | vol3 | underwater | tight | +0.3% | 11% | 1.4 | 2.4 |
| 28 | w1_38_rocx_sp_ratchet | rocx | spot | vol3 | classic | ratchet_only | -0.3% | 32% | 4.9 | 4.8 |
| 29 | w1_29_macdx_trend3_cl | macdx | lev | trend3 | classic | default | -15.0% | 98% | 8.0 | 3.9 |
| 30 | w1_30_macdx_vol3_cl_mh1 | macdx | lev | vol3 | classic | tight | -20.2% | 99% | 9.1 | 3.2 |
| 31 | w1_28_macdx_vol3_uw_t | macdx | lev | vol3 | underwater | tight | -23.6% | 99% | 8.7 | 9.0 |
| 32 | w1_32_rocx_trend3_cl_r | rocx | lev | trend3 | classic | ratchet_both | -27.5% | 98% | 8.9 | 1.9 |

No survivors / negative holdout: w1_03_v7_vol3_uw_t, w1_04_v7_vol3_cl_t, w1_05_v7_trend3_uw_d, w1_06_v7_trend3_cl_t, w1_07_v7_vol37d_cl_d, w1_08_v7_vol37d_uw_t, w1_10_v7_vXt9_uw_d, w1_13_v7_vol3_uw_mh1, w1_14_v7_vol3_cl_t_mh1, w1_16_v7_vol3_cl_dd35, w1_19_prime_vol3_cl_t, w1_26_sx1_vol3_cl_d, w2_ref_01_v7_vol3_uw_d, w2_ref_02_v7_vol3_cl_d, w2_ref_12_v7_vol3_ww_t, w2_ref_09_v7_volume3_cl_d, w2_ref_18_prime7_trend3_cl