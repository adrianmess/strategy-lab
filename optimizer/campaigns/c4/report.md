# Campaign c4 — report
updated 2026-07-21 01:35

Ranked by OOS-best holdout %/mo (the honest number). tpm = trades/month; prefer high tpm + modest %/trade (many-small-gains goal). Verify with walk-forward before adopting.

| rank | spec | strat | mode | method | scoring | space | holdout %/mo | dd | tpm | mh(d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | m_lev_vol3 | metax | lev | vol3 | classic | default | +12.8% | 48% | 2.6 | 3.9 |
| 2 | w2_refine_m_lev_vol3 | metax | lev | vol3 |  |  | +12.6% | 54% | 2.6 | 3.7 |
| 3 | m_spot_vol3 | metax | spot | vol3 | classic | default | +10.3% | 31% | 1.5 | 4.1 |
| 4 | w2_refine_m_spot_vol3 | metax | spot | vol3 |  |  | +10.3% | 31% | 1.5 | 4.1 |
| 5 | m_spot_vt9 | metax | spot | vt9 | classic | default | +7.7% | 31% | 1.9 | 4.1 |
| 6 | w2_refine_m_spot_vt9 | metax | spot | vt9 |  |  | +7.7% | 31% | 1.9 | 4.1 |
| 7 | m_lev_trend3 | metax | lev | trend3 | classic | default | +6.2% | 40% | 4.1 | 6.1 |
| 8 | m_spot_trend3 | metax | spot | trend3 | classic | default | +2.8% | 32% | 6.5 | 6.5 |
| 9 | w2_refine_m_spot_trend3 | metax | spot | trend3 |  |  | +1.5% | 32% | 8.1 | 6.5 |
| 10 | m_spot_month12 | metax | spot | month12 | classic | default | -3.1% | 56% | 2.4 | 6.8 |
| 11 | m_lev_vt9 | metax | lev | vt9 | classic | default | -3.1% | 96% | 2.6 | 3.9 |
| 12 | w2_refine_m_lev_trend3 | metax | lev | trend3 |  |  | -14.7% | 96% | 2.4 | 2.5 |
| 13 | m_lev_month12 | metax | lev | month12 | classic | default | -28.6% | 95% | 1.1 | 2.5 |

No survivors / negative holdout: none