# Spot-focus research: continuous adaptation, proven methods, and the 1%/day question

## Addendum (2026-07-27): perp-vs-spot basis measurement

All research data (every pair, both modes) is MEXC **perp** candles; spot backtests are spot
rules on perp prices, and the live spot trader signals off perp but fills on the spot book.
Measured on 23,813 aligned 1-minute bars (2026-07-01 → 07-27, MEXC spot klines vs research perp
data), SOL/USDT: spot trades ~+5.5 bps above perp, very stable (std 1.8 bps; |basis| p95 8.2,
p99 9.5, max 34.8 bps during the 07-10 spike; no meaningful widening in high-vol terciles).
Because the basis level cancels across a round trip, the per-trade PnL error is
**p50 1.3 bps / p95 4.9 bps — versus 10 bps round-trip fees**, i.e. at worst about half a fee
side, and %-based signals are unaffected by the constant offset. Verdict: perp candles are a
sound research proxy for SOL spot; assumption documented rather than re-engineered. If deep
true-spot history is ever wanted, CoinAPI carries MEXC_SPOT_* from 2023-11-23 (MEXC's own spot
kline API only retains ~30 days).
*2026-07-27 — for Strategy Lab (MEXC SOL/USDT spot, 3-min bars, ~0.05%/side fees)*

## 1. Verdict on the 1%/day goal

**Sustained 1%/day is not a realistic target.** It compounds to ~1,100–3,000%/yr; every credible
source treats it as marketing math, not an achievable average. Sizing/liquidity constraints and
fee drag bound intraday returns; a *very good* retail algorithmic operation lands around
**0.1–0.3%/day average** (≈ 30–100%/yr) with real drawdowns. ([DayTrading.com](https://www.daytrading.com/1-percent-a-day),
[Sahm Capital](https://www.sahmcapital.com/news/content/can-you-really-earn-1-daily-from-trading-dont-get-excited-until-you-do-the-math-2026-01-15),
[The Robust Trader](https://therobusttrader.com/can-you-make-1-percent-a-day-trading-how-much-can-you-make-per-day/))

**Context: the lab is already near the credible ceiling.** The spot MetaX router walks forward at
+7.6%/mo (~0.25%/day) and the stacked router at +13.2%/mo chained OOS (~0.41%/day). Those numbers,
if they hold live, are already *above* what the literature considers sustainable for retail intraday
systems. The honest goal is not "3× the return" but "keep the return while cutting drawdown and
proving persistence live." Occasional 1% days will happen; a 1%/day *average* will not.

## 2. Continuous adaptation: what the evidence says

- Adaptive moving averages (KAMA, FRAMA, MESA/MAMA, VIDYA) have thin independent evidence. The one
  academic comparison found ([MPRA 94323](https://mpra.ub.uni-muenchen.de/94323/1/MPRA_paper_94323.pdf))
  tested 15 months of FX **with zero commission** — not probative for fee-paying 3-min crypto.
  Vendor/practitioner material is marketing-grade; no credible study shows adaptive-length
  indicators beating well-chosen fixed/regime-switched parameters out of sample.
  ([LuxAlgo comparison](https://www.luxalgo.com/blog/kama-vs-frama-comparing-adaptive-moving-averages/),
  [TrendSpider on KAMA](https://trendspider.com/learning-center/what-is-the-kaufman-adaptive-moving-average/))
- **Our own experiment agrees**: campaign c6 (cvol7, 7-grade continuous interpolation, endpoint
  parameterization with *fewer* knobs than vol3) tested **worse** than the discrete 3-bucket
  champions out of sample in 9 of 10 family×mode cells. Best cadapt spot result: macdx +5.7%/mo
  OOS vs the discrete router's +7.6%/mo WF.
- Conclusion: **continuous adaptation of indicator parameters is not the path.** The market appears
  to reward a few *distinct* behavioral modes more than smooth parameter morphing. cadapt stays
  available in the UI as a research option, but I recommend no further compute on it.

## 3. What has real evidence, ranked

### Tier 1 — strong evidence, direct fit
1. **Meta-labeling (López de Prado)** — a secondary model that doesn't pick trades but decides
   *whether to take / how much to size* the primary strategy's signals. Documented OOS precision
   gains (0.48→0.54) and improved strategy performance in the Hudson & Thames replication;
   widely adopted in professional quant shops.
   ([Hudson & Thames study](https://hudsonthames.org/wp-content/uploads/2022/04/Does-Meta-Labeling-Add-to-Signal-Efficacy.pdf),
   [Wikipedia overview](https://en.wikipedia.org/wiki/Meta-Labeling))
   *Fit:* the lab logs every simulated trade with rich causal context (vol percentile, trend,
   bucket, hour, recent streak). A small classifier over those features, gating/sizing the live
   router's entries, is the highest-EV upgrade available. It also directly serves the
   drawdown-cutting goal.
2. **Position sizing as its own dimension** — fractional Kelly / volatility targeting applied to
   *fraction of equity per trade* (spot's only sizing lever; currently all-in). Solid evidence it
   improves risk-adjusted growth; ¼–½ Kelly is standard practice.
   ([Kelly in quant practice](https://coriva.eu.org/en/kelly-criterion-position-sizing/),
   [position-sizing guide](https://mbrenndoerfer.com/writing/optimal-position-sizing-kelly-criterion-leverage))
   *Fit:* engines assume full-equity entries; adding a per-trade size fraction (driven by
   meta-label confidence and/or realized-vol target) is a moderate engine change with honest
   backtest support.
3. **Selection hygiene: PBO / Deflated Sharpe** — with 280+ runs on disk, the top backtest is
   partly a lottery winner. CSCV/PBO and DSR quantify how much.
   ([Bailey & López de Prado, DSR](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf),
   [PBO paper](https://www.researchgate.net/publication/318600389_The_probability_of_backtest_overfitting))
   *Fit:* add a PBO/DSR panel to campaign reports; cheap and permanently raises the honesty bar.

### Tier 2 — good evidence, medium effort
4. **HMM regime detection** — 4-state non-homogeneous HMMs show the best OOS forecasting across
   crypto studies; k-means+HMM hybrids beat standalone models.
   ([Bayesian HMM predictability](https://arxiv.org/pdf/2011.03741),
   [HMM regime changes in cryptoassets](https://onlinelibrary.wiley.com/doi/abs/10.1002/qre.2673),
   [k-means+HMM hybrid](https://jdmdc.com/index.php/JDMDC/article/view/57))
   *Fit:* a causal `hmm4` bucketing method for MetaX/stack — same walk-forward gates. Regime
   *detection* upgrades have better evidence than parameter *morphing*.
5. **Intraday time-of-day structure** — robust, replicated: activity/vol/liquidity peak 16:00–17:00
   UTC; US+London overlap = best liquidity; the "weekend effect" concentrates in Sunday 23:00 UTC.
   ([Periodicity in crypto vol/liquidity](https://arxiv.org/pdf/2109.12142),
   ["tea time" study](https://link.springer.com/article/10.1007/s11156-024-01304-1),
   [QuantPedia BTC anomalies](https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/))
   *Fit:* hour-of-day as a meta-label feature (safest), or an explicit trading-hours gate.
   Unlike month12 seasonality, this has cross-market published support — still validate WF.
6. **BTC→alt lead-lag** — documented minute-scale delayed response of alts to BTC moves
   (strongest in small caps; SOL is large-cap so expect a weak edge, but BTC momentum as a
   *feature* costs little).
   ([Springer high-frequency price transmission](https://link.springer.com/article/10.1007/s10690-026-09589-z),
   [lagged-effect study](https://dergipark.org.tr/en/download/article-file/2206815))
   *Fit:* add BTC 1–5-min returns to the meta-label feature set (needs a BTC candle feed).

### Tier 3 — plausible, weaker/practitioner-grade evidence
7. **VWAP / volume-node mean reversion** — practitioner-documented profitability after fees in
   ranging conditions; fits ScalpX's VRVP/POC heritage (which tested edgeless standalone — a
   meta-labeled, hour-gated revival is the only version worth trying).
   ([VWAP reversion](https://crosstrade.io/learn/trading-strategies/vwap-reversion))
8. **Order-flow imbalance** — genuinely predictive at 50ms–5min horizons but needs order-book
   data, not OHLCV; MEXC spot API polling won't capture it faithfully. Park unless we add a
   book-snapshot collector. ([OFI overview](https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html),
   [crypto microstructure](https://arxiv.org/html/2602.00776v1))

## 4. Recommended plan (spot-only)

- **Phase 1 — Meta-label + size (highest EV):** build the trade-context dataset from existing
  backtest/WF logs of the spot router + stack; train a small gradient-boosted classifier
  (purged walk-forward CV, no leakage); use it live as (a) skip-filter and (b) size fraction
  (¼-Kelly-capped, vol-targeted). Success bar: WF return ≥ current with materially lower dd,
  or higher return at same dd.
- **Phase 2 — hmm4 bucketing** for MetaX + stack; compare WF vs vol3/vt9. Time-of-day feature
  into the meta-label (already in Phase 1's feature set).
- **Phase 3 — BTC feed + lead-lag feature; POC mean-reversion + meta-label** as a new component
  candidate for the router.
- **Throughout:** PBO/DSR panel in campaign reports; all adoption still gated by chronological
  walk-forward; live flips remain Adrian-only.

**Expectation setting:** Phase 1–3 done well might lift the honest spot number from ~0.25–0.4%/day
toward ~0.5%/day with lower drawdown. Nothing in the evidence supports promising 1%/day.
