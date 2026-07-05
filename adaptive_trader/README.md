# Adaptive Trader — V5.2 strategy with regime adaptation

> **UPDATE (round 2):** the trader now runs the V6 candidate configs from the
> optimizer. `config.json` = leveraged no-stop config (dry-run by default),
> `config_spot.json` = spot long-only config. `refit.py` re-optimizes every 28
> days. Reusable optimizer CLIs live in `../optimizer/` (multi-process,
> `--procs N`, unlimited runtime). Results browser: `../dashboard/index.html`
> and `../dashboard/backtests.html`. Round-2 details: `research2/README.md`.
> Parity tests: `test_parity.py` (legacy) and `test_parity_v6.py` (current).

Turns the V5.2 Pine strategy into a standalone live-trading app that adapts its
parameters to market conditions (volatility regimes), sized so that liquidation
is structurally out of reach. Executes through the existing Playwright webhook
server (`../webhook_server.py`).

## What was done

1. **Replicated the Pine strategy in Python** (`research/engine.py`) with
   TradingView execution semantics (signals on bar close, fills next bar open,
   0.04%/side commission).
2. **Validated it** against your TradingView export: 325 of 351 trades in the
   comparable window match *exactly* (timestamp, direction, subsystem); the
   rest are explained by CoinAPI data gaps (Jun–Nov 2024, a few smaller) and
   ±1-bar feed differences. Zero logic mismatches.
3. **Backtested on CoinAPI data** (MEXCFTS_PERP_SOL_USDT, 3-min, Nov 2023 →
   Jul 2026, `research/data/`).
4. **Diagnosed the trade-drought problem**: the MACD entry thresholds are set
   in fixed price-% / absolute units. Their firing rate collapses ~10x in
   low-volatility markets (that's why the strategy goes quiet). Also: the
   MACD-cross **long** subsystem never fires at all (requires `macd < -99`).
5. **Fixed it with volatility normalization**: MACD signals are z-scored
   against their own trailing 3-day volatility, so trigger rates are
   regime-invariant. Per-regime knobs (thresholds, profit-target scale,
   leverage) are then optimized per volatility tercile (low/mid/high, causal
   trailing 30-day percentiles).
6. **Walk-forward validated** (10 folds, 2-month test windows, ~19 months
   truly out-of-sample): **+3.6x total (~6.9%/month), 8–18 trades/month in all
   regimes** including the quiet 2026 months where the original took 1–8
   trades. 4 protective-stop hits in 19 months, each costing ~25% equity, no
   liquidations, worst fold drawdown 37%.

## The uncomfortable findings (please read)

- Your TradingView backtest "never hits the stop" partly by **feed luck**. On
  CoinAPI's MEXC data the *same default parameters* fire one extra short on
  **2024-03-13** which runs -34% against the position. With your live config
  (stop off, 8x) that is a **liquidated account on 2024-03-14**. TradingView's
  feed misses this trade by one bar.
- Even in the surviving window, defaults came within 2.3% of liquidation twice
  (Feb 2026, Jan 2025 shorts with ~-9.9% excursions at 8x).
- Conclusion built into this app: **the stop must stay on live**, and leverage
  must be low enough that a stop-out is survivable. The optimizer enforces:
  `leverage × stop ≤ 30% equity damage` and `stop ≤ 0.7 × liquidation distance`.
  The validated config uses ~2.2–2.4x leverage with a 10% stop.

## Files

| File | Purpose |
|---|---|
| `trader.py` | Main loop: feed → signals → webhook execution. Dry-run by default. |
| `strategy.py` | Live signal evaluator (same code path as the validated backtest). |
| `data_feed.py` | MEXC public kline feed (1m, resampled to 3m; no API key needed). |
| `config.json` | Production parameters + risk dial + webhook URL. |
| `test_parity.py` | Proves live evaluator == research engine (82/82 trades match). |
| `research/` | Full research stack: engine, fast (numba) engine, regime code, walk-forward optimizer, 2.6y of CoinAPI 3m/1m data. |

## Running

```bash
pip install -r requirements.txt

# 1) parity check (should print PARITY: PASS)
python3 test_parity.py

# 2) dry run — watches the market, logs the trades it WOULD take
python3 trader.py

# 3) live — start webhook_server.py first, then:
python3 trader.py --live
```

Set `equity_usdt` in `config.json` to the account equity you allocate to this
strategy (it sizes orders as `equity × leverage / price`).

## Risk dial

`risk_dial` in config.json scales leverage:

| dial | eff. leverage | OOS growth | worst stop-out | worst fold DD |
|---|---|---|---|---|
| 1.0 | ~2.2–2.4x | ~6.9%/mo | ~-25% equity | 37% |
| 2.0 | ~4.4–4.8x (cap 5) | ~11.1%/mo | ~-50% equity | 67% |

Anything above dial 2.0 re-creates the liquidation exposure this project was
built to remove. There is no setting that reproduces your old 8x behavior —
that's deliberate.

## Re-optimization (recommended monthly)

```bash
cd research
python3 download_data.py            # extend CoinAPI history (edit dates/key inside)
python3 -c "from optimize import *; ..."   # or re-run the production search:
python3 - <<'EOF'
import json, numpy as np
from optimize import get_variants, sample_candidate, eval_candidate, feasible, average_candidates
variants = get_variants(force=True)
rng = np.random.default_rng()
feas = []
for _ in range(3500):
    c = sample_candidate(rng)
    m = eval_candidate(c, variants, None, '2099-01-01')
    if feasible(m, c): feas.append((m['score'], c, m))
feas.sort(key=lambda x: -x[0])
avg = average_candidates([c for _, c, _ in feas[:5]])
print(json.dumps(avg, indent=2))   # paste into ../config.json "params"
EOF
```

## Known limitations

- **Funding fees are not modeled** (positions are mostly minutes–hours, but
  multi-day holds do occur; MEXC SOL funding is typically ±0.01%/8h).
- Playwright execution adds seconds of latency vs the backtest's next-bar-open
  fill; at 3-minute bars this is minor but real.
- CoinAPI history has holes (Jun–Nov 2024, ~4 days Nov 2025); results are
  computed on the available ~25 months.
- Backtest compounding numbers assume full-equity reinvestment every trade;
  treat the *monthly growth* figures as the meaningful unit, not the
  cumulative multiples.
- Past regime behavior does not guarantee future behavior. The walk-forward
  procedure is the defense: re-run it monthly so parameters track the market.
