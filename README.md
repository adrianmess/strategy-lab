# SOL Strategy Lab

Research, optimization, backtesting and live-trading platform for SOL/USDT
strategies on MEXC (3-minute timeframe). Built around the V5 TradingView
strategy family and operated entirely through a local website.

> Forked from `mexc-td-enhanced` (the original Playwright webhook executor
> project) and expanded with the research stack, optimizer, live trader and
> web UI. The original executor (`webhook_server.py`) is included unchanged
> (its original README: `README_original_executor.md`).

## Quick start

```bash
pip install -r adaptive_trader/requirements.txt
python3 panel/server.py          # -> http://127.0.0.1:8800
```

Everything is done from the site:

| Page | What it does |
|---|---|
| **Control panel** (`/`) | Start/stop the live trader (dry-run or LIVE), MEXC executor, trader settings, environment doctor |
| **Optimize** | Full-parameter searches (incl. indicator lengths), per-regime specialists, editable ranges, AI advisor, backtest previews |
| **Backtests** | Every published backtest: equity curves, monthly returns, trade lists, gap-handling badges |
| **Research** | The walk-forward study that established what works (and what liquidates) |
| **Docs** | Glossary of every concept, metric, parameter and workflow |

## Layout

```
panel/            control-panel web server (Flask) + env doctor + site tests
dashboard/        the website pages (served by the panel)
optimizer/        search CLIs (optimize2_cli, walkforward_cli, backtest_cli, ai_advisor)
adaptive_trader/  live trader, strategy evaluators (V6/V7), refit loop
  research/       validated V5 engine port + data (CoinAPI parquets)
  research2/      V7 engine (searchable indicator lengths), optimizer core, study artifacts
webhook_server.py the original MEXC Playwright executor (unchanged)
```

## Strategy lineage

- **V5 (original)** — your TradingView Pine strategy, validated bar-for-bar
  against your exports (325/351 exact trade matches).
- **V5 family / V6** — volatility-normalized thresholds (z-scored MACD).
- **V5 family / V7** — every parameter searchable, per-regime specialist sets.
- **Scalp family / ScalpX** — the VRVP+CVD+EMA scalp strategy (tested: no edge).

## Read before trading

The leveraged mode deliberately has **no stop loss** (matching the original
live style) — liquidation risk is real and documented throughout the site.
All honest performance numbers come from holdout/walk-forward evaluation, not
full-history fits. See Docs -> "Risk & honesty".
