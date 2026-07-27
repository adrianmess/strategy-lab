# Campaign c6 — report
updated 2026-07-26 22:05

Ranked by OOS-best holdout %/mo (the honest number). tpm = trades/month; prefer high tpm + modest %/trade (many-small-gains goal). Verify with walk-forward before adopting.

| rank | spec | strat | mode | method | scoring | space | holdout %/mo | dd | tpm | mh(d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | ca_rocx_lev | rocx | lev | cvol7 | classic | levsafe | +18.2% | 69% | 6.2 | 20.1 |
| 2 | w2_ref_rocx_lev | rocx | lev | cvol7 | classic | levsafe | +18.2% | 69% | 6.2 | 20.1 |
| 3 | ca_macdx_spot | macdx | spot | cvol7 | classic | default | +5.3% | 17% | 8.5 | 4.2 |
| 4 | ca_v7_spot | v7 | spot | cvol7 | classic | default | +3.6% | 15% | 9.2 | 4.9 |
| 5 | ca_prime7_spot | prime7 | spot | cvol7 | classic | default | +2.6% | 6% | 2.7 | 2.3 |
| 6 | w2_ref_rocx_spot | rocx | spot | cvol7 | classic | default | +1.9% | 43% | 11.5 | 14.1 |
| 7 | ca_scalpx2_spot | scalpx2 | spot | cvol7 | classic | default | +0.8% | 14% | 5.9 | 5.7 |
| 8 | ca_rocx_spot | rocx | spot | cvol7 | classic | default | +0.7% | 31% | 14.3 | 6.9 |
| 9 | ca_v7_lev | v7 | lev | cvol7 | classic | levsafe | +0.5% | 48% | 20.8 | 2.2 |
| 10 | w2_ref_v7_spot | v7 | spot | cvol7 | classic | default | -1.7% | 23% | 11.5 | 11.5 |
| 11 | w2_ref_prime7_spot | prime7 | spot | cvol7 | classic | default | -2.3% | 15% | 9.2 | 6.9 |
| 12 | ca_prime7_lev | prime7 | lev | cvol7 | classic | levsafe | -4.2% | 32% | 5.1 | 2.4 |
| 13 | w2_ref_scalpx2_spot | scalpx2 | spot | cvol7 | classic | default | -6.0% | 59% | 2.4 | 6.8 |
| 14 | ca_macdx_lev | macdx | lev | cvol7 | classic | levsafe | -16.4% | 92% | 8.4 | 2.3 |

No survivors / negative holdout: ca_scalpx2_lev