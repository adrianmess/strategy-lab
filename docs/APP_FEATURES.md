# Panel & trading features — how things work and where they're configured

Reference for everything built in the 2026-08-24..27 sessions. The panel UI
is `/terminal` on the mini (`panel/terminal.html` + `panel/server.py`);
trading logic lives in `adaptive_trader/`. Companion docs:
`PERF_IMPROVEMENTS.md` (CPU options), `EC2_OFFLOAD_RUNBOOK.md`, `HANDOFF.md`.

## P&L accounting (Overview + History)

- **Event source**: `/api/pnl_events` — realized P&L events for ALL FOUR
  account x market combos (mexc1/mexc2 x futures/spot), 30d, cached 60s.
  Futures = MEXC's own `realised` per closed position; spot = FIFO-matched
  fills (a sell of coins with no recorded buy — i.e. deposited coins —
  contributes NOTHING; conversions on stable pairs are excluded).
- **Origin attribution**: each event is labeled `bot` if a trader recorded a
  close of that symbol on that account+market within ±4 min
  (notifications.log, configs ending .json), else `manual`. Caveat: bot
  trades closed while a trader was down have no notification and read as
  manual.
- **Origin switch**: selector on the Cumulative card ("bot + manual / bot
  only / manual only", persisted as `slOrigin`). Drives KPIs, cumulative
  chart, daily chart, by-pair, and honors the History exclude flags.
- **Deposits/withdrawals never count as P&L**: `/api/flows` (credited
  deposits + successful withdrawals, USDT-valued, cached 5 min) is
  subtracted when reconstructing balances.
- **KPI % = Modified Dietz** (`dietzPct` in terminal.html): window P&L ÷
  average capital at risk, where average capital = window-start balance +
  each external flow weighted by the fraction of the window it was present.
  Deposited-then-withdrawn money counts only for its days in the account.
  Numerator follows the origin filter; capital base is the whole pool (both
  origins trade the same wallets). Dollar figures are always exact sums.
- **TWR 30d** (Daily card footer): daily flow-adjusted returns compounded —
  the skill-pure number, immune to deposit/withdrawal timing.
- **Exclusions**: History page Exclude/Restore per trade
  (`panel/ignored_trades.json` via `/api/ignore`); excluded trades stay
  listed but leave every aggregate.
- **History page**: exchange-truth closes for all four combos with Origin
  column (BOT/MANUAL), account filter incl. instance-less combos.

## Trade page

- **Chart**: TradingView lightweight-charts (vendored), MEXC klines proxied
  via `/api/klines` (1000 bars, ~400-bar default view). OHLC legend +
  volume pane; EMA 5/10/30/60 overlay (on by default); MACD / RSI / BB %B
  sub-panes (toggles persisted); DST-aware time zone picker (default
  America/Los_Angeles, `tdTz`); pan/zoom preserved across the 12s refresh;
  widening past the data edge refits.
- **Order panel**: both accounts x Leverage/Spot; leverage presets, margin
  max, limit price; live per-pair fee line with estimated order cost;
  preflight balance checks pointing at Transfer; persistent result box.
- **Positions tabs** (MEXC-style): Positions / Open Orders / Position
  History / Order & Trade History / Assets (`/api/trade/history`, 30s
  cache). Positions shows MANUAL trades only (strategy-held rows live in
  Bot positions below). "All accounts" vs selected-account scope persisted.
- **Per-row actions**: Flash Close; TP/SL modal (entire/partial position,
  trigger price <-> ROE% linked); Close cell (price blank=market + USDT
  qty); ⇄ on-close transfer; armed TP/SL and transfer rules render as tags
  with computed trigger prices in BOTH views.
- **Source tags**: SHADOW ADOPTED / SHADOW AUTO (purple, adoption
  machinery), SHADOW LATE-JOIN (orange, joined an already-open virtual
  trade; runner records `late_join`, panel infers for older positions from
  virtual-entry-vs-open-time > 5 min), BOT SIGNAL, Manual.
- **Bot positions section**: each running instance's REAL position —
  symbol, side, source, qty, market entry, now, uPnL, projected close,
  auto-close threshold. Virtual/shadow trades display on the Overview
  shadow expander (with projected close + Adopt).
- **Projected close**: engines publish their live TP/SL for the open
  virtual trade (scalp bracket prices; e3/macdx decayed targets replicated
  at the last bar) -> host bar msg (`exit_proj`) -> runner state -> panel.
- **Speed**: parallel exchange legs; `/api/trade/positions_all` kept warm
  every ~12s by a background thread; client paints instantly from a
  localStorage snapshot (`tdSnap`).

## Panel-side automation (watchers in server.py)

- **TP/SL watcher** (`panel/tpsl.json`, 20s): margin-basis % for futures,
  price-move % for spot; entire or partial (`close_pct`); market-closes and
  WhatsApps on fire. One-shot.
- **On-close transfer rules** (`panel/xfer_rules.json`, 30s): when THIS
  position closes, move USDT between the account's own wallets
  (fut2spot/spot2fut), amount or max; WhatsApp on fire. One-shot; never a
  withdrawal.
- **Manual-close alerter**: diffs warm position snapshots; any manual
  (non-bot) position that disappears -> WhatsApp + alert-stream note, with
  realized P&L for futures. Deduped against UI closes/TP-SL (120s window);
  skips rounds where that account's API leg errored.
- **Notifications/toasts**: traders write notifications.log; manual trades
  now write compatible lines (`config = "manual (acct market)"`), so
  `/api/trades` toasts cover bot AND manual opens/closes.

## Shadows, adoption, late-join (fcfs_runner.py)

- **Armed per-shadow rules**: adopt at <= adopt_pct, optional close at >=
  close_pct (sidecar `.armed_<statefile>`). One-shot, survive restarts.
- **Auto-adopt (standing)**: while flat, adopt the deepest shadow at/below
  threshold; per-pair depths; anti-churn (same trade re-adopts only 1%
  below last exit); optional auto-close.
- **Presets are PER CONFIG** (`/api/adopt_rec`): immutable researched
  DEFAULT (spot: vol-scaled depths + close +2; lev: flat -3 all pairs, no
  close) + user SAVED set (`panel/adopt_recommended.json`). Buttons fill the
  form; Enable auto arms whatever is visible — no save needed.
- **Late-join**: legacy joins of already-open virtual trades (red-only +
  liq-distance cap on lev; any color on spot). SUPPRESSED whenever
  auto-adopt is enabled (adopt-sim: baseline never joins open trades;
  unthresholded joins underperform). Research: spot winner = vol-scaled
  depths + close +2 (x83.5k vs x44k); lev winner = flat -3 margin, all
  pairs, mirror exit (full + walk-forward; per-pair depths were overfit).

## Fees

- `adaptive_trader/fees.json`: refreshed hourly by the panel from MEXC
  (futures: public contract detail per pair; spot: signed tradeFee).
  `research2/fees_live.per_side(mode, coin)` feeds wf2/optimizer2/
  metax_cli/pairx_cli and the LIVE metax evaluation; falls back to the
  historic 0.04%/0.05% constants. MacBook agent syncs the file. Several
  pairs are currently 0-fee promos. `/api/fees` + live fee line on Trade.

## Signal latency & reliability

- **Bar-aligned polling** (fcfs_host.py): dense jittered retry ladder right
  after each bar boundary, px-heartbeat naps mid-bar.
- **WebSocket wake-up**: contract WS kline stream as a TRIGGER only (new
  candle => previous closed => immediate REST fetch); REST stays the source
  of truth; silent fallback to polling. Median signal->fill went 13-16s to
  ~5-8s; the remaining floor is engine eval (2-5s) + 1.5s arbitration.
- **Traders detach from launchd** (`start_new_session` in the panel's
  trader spawn): the watchdog's `launchctl remove` reaped panel-spawned
  traders on 2026-08-26; setsid makes panel restarts truly safe again.
- Hosts auto-respawn on death (runner supervision); simultaneous backfills
  ride out MEXC 510 rate limits via the retry ladder.

## Money movement & account views

- **Transfer/Convert** (modal, typed confirms): spot<->futures USDT within
  an account; USDC->USDT convert. No withdrawal capability anywhere.
- **Deposit section**: per-instance QR/address/memo, coin+network chips,
  recent deposits incl. in-flight.
- **Accounts card**: FULL total per account (spot + futures stablecoin
  equity) with per-wallet breakdown.
- **Sync bandwidth cap**: Progress page control -> `/api/sync_limit`
  (panel/sync_limit.json); MacBook agent + mac2_sync read it live.

## Key files & endpoints

- Panel sidecars: `tpsl.json`, `xfer_rules.json`, `adopt_recommended.json`,
  `sync_limit.json`, `instances.json`, `ignored_trades.json`,
  `panel_key.json` (off-box UI key: append `?k=<key>` once).
- Trader sidecars (adaptive_trader/): `.armed_<statefile>`,
  `.adopt_<statefile>`, `fees.json`, `notifications.log`, state/log files
  per config.
- Main new endpoints: `/api/pnl_events`, `/api/flows`, `/api/trade/state`,
  `/api/trade/positions_all`, `/api/trade/history`, `/api/trade/order`,
  `/api/tpsl`, `/api/xfer_rule`, `/api/adopt_rec`, `/api/shadow_arm`,
  `/api/fees`, `/api/klines`, `/api/transfer`, `/api/convert`,
  `/api/deposit/*`, `/api/sync_limit`.
- All order/transfer/arm actions require explicit confirms
  (TRADE/TRANSFER/CONVERT/ARM); LIVE traders are only ever enabled by
  Adrian in the panel.
