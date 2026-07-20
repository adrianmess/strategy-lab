# Strategy Lab — Session Handoff

_Last updated: 2026-07-13, early morning. Written for a fresh session picking up this project cold._

---

## 1. PROJECT OVERVIEW

**Strategy Lab** (`/Users/adrian/Code/strategy-lab`) is Adrian's personal platform for
optimizing, backtesting, and (now) **live-trading** his TradingView Pine strategies on
**MEXC SOL_USDT perpetual futures, 3-minute chart**.

The workflow it supports end-to-end:
1. **Port** a TradingView Pine strategy into a fast Python engine, validated bar-for-bar
   against the strategy's TradingView CSV/XLSX export.
2. **Optimize** its parameters with a multi-process genetic/random/refine/cross-fit search
   that has serious anti-overfitting machinery (holdouts, reservoir sampling, honest
   feasibility gates).
3. **Backtest** any config over full continuous history, published to a dashboard.
4. **Adopt** a chosen config into a live trader that watches MEXC live and places orders by
   **driving the MEXC website via Playwright** (MEXC has no retail futures order API).

**Broader priority right now:** Adrian wants to **go live with a leveraged strategy**. Most
of this session's later half was building out the live-trading execution path (proxy,
headless browser, live price feed, control-panel wiring) to get to a safe first live run.
A parallel thread was a **spot-mode research campaign** to find day-trade/scalp configs.

**Two repos, do not confuse them:**
- `/Users/adrian/Code/strategy-lab` — THE working project. All edits happen here.
- `/Users/adrian/Code/mexc-td-enhanced` — the ORIGINAL source project that the Playwright
  webhook server was derived from. **Read-only reference.** We copied its updated
  `webhook_server.py` into strategy-lab this session, but never modify mexc-td-enhanced.

**Bash path mapping** (sandbox ↔ Mac): the Linux sandbox sees the repo at
`/sessions/festive-happy-gauss/mnt/Code/strategy-lab/` — same files, different path prefix.

---

## 2. WORK COMPLETED (this session, in order)

### A. Dashboard/UX improvements (early session)
1. **Backtests page — total % gain in the detail header.** When you click a backtest row,
   the header shows total gain (final equity ÷ start − 1), green/red, hidden for liquidated
   runs, exponential notation past 10,000%. File: `dashboard/backtests.html`.
2. **Backtests page — "total %" sortable column** added next to "growth/mo". Liquidated rows
   show "—" and sort to the bottom. `dashboard/backtests.html` (COLKEYS, header, colspan
   bumped 16→17).
3. **Mark-as-"best" feature** on BOTH the Optimize runs list and the Backtests list. A ☆/★
   button; marked rows get a soft-green tint + green left border. Backend: runs store a
   `marked_best` marker file inside the run dir (survives rename, dies with delete);
   backtests store a `best:true` flag in the entry. Endpoints `/api/runs2/mark`,
   `/api/backtests/mark` in `panel/server.py`. UI in `optimize.html`, `backtests.html`.
4. **Full-width layout** — removed `max-width` caps on both `backtests.html` (main) and
   `optimize.html` (.layout).
5. **Optimize "Stop at score"** field — ends a search early once best TRAIN score reaches a
   value. Arg `--stop-score` in `optimize2_cli.py` (checked each generation in the main loop
   AND the cross-fit search phase), passthrough in `panel/server.py`, field + tooltip in
   `optimize.html`. NOTE: this watches the in-sample training score, which is exactly the
   overfit-prone number — it's a "stop polishing overfits" lever, not a quality target.
6. **Persist launch settings** — the full Optimize launch card is saved to localStorage on
   "Start search" and restored on load (`optLaunchSettings`). `optimize.html`.
7. **"Load setup" button on each run row** — fills the launch card from that run's recorded
   settings (strategy, mode, method, algo, holdout, DD, scoring, anchor, etc.), scrolls up.
   `optimize.html` `loadRunSettings()`.
8. **Holdout column shows total %** next to monthly % (with a tooltip clarifying it's the
   HOLDOUT-window total, which is much smaller than the full-history total shown on the
   Backtests page — this caused an "why do the two totals differ" question). Also added
   `total_mult` to backtest flags in `backtest_cli.py` so the `bt … full` link lines show a
   total too. `optimize.html`, `backtest_cli.py`.
9. **Live progress bar in the runs list.** While an optimize job runs, that run's settings
   column shows a filling progress bar + %/ETA/evals, parsed from `progress.json` (which the
   optimizer already writes every generation). Server attaches `progress`/`running` to
   `/api/runs2`; UI polls every 5s while anything runs. `panel/server.py`, `optimize.html`.
10. **Trades table — "% gain" column** (price move in trade direction, from stored
    entry/exit prices so it works retroactively; leverage impact on hover; open positions
    show unrealized move). `dashboard/backtests.html`.

### B. Combine-runs feature
11. **Merge / breed two optimizer runs into one.** `--resume-from` now accepts a
    comma-separated list of runs; every imported candidate (pool + reservoir) is
    **re-evaluated under the new run's settings/gates** so runs searched with different
    max-DD/scoring/windows combine honestly. `--merge-mode merge|breed`: "breed" keeps
    genetic parents balanced across the source runs so cross-run hybrids get bred.
    `optimize2_cli.py`, `panel/server.py`, plus a "Combine runs" UI row in `optimize.html`
    with type-to-filter datalist inputs, genome-compatibility validation, and starred runs
    floated to the top of the picker.
12. **Combine bugfixes** discovered when Adrian's merges misbehaved: (a) auto-name was
    truncated to 40 chars, collapsing `merge_A_B` → a fixed string that silently RESUMED one
    junk run every time; fixed to strip stacking `merge_` prefixes, 60-char names, dedupe
    with `_2/_3` suffixes; (b) a stale `stop-score` in saved settings ended merges after one
    generation (loud warning added on arm); (c) combine is single-use, now says
    "combine consumed" after launch.

### C. Per-mode parameter spaces (spot vs lev)
13. **Separate parameter spaces per mode.** Each strategy's existing entry in
    `param_space.json` is the LEV space; a `<strategy>@spot` sibling is the SPOT space
    (created lazily as a copy). The Optimize panel swaps which space it shows/edits based on
    the Mode dropdown (green "SPOT ranges" / red "LEV ranges" badge). Indicator-length
    variant libraries are engine-shared and stay only on the base entry. Backend resolves
    `<strategy>@spot` when `--mode spot`. Files: `optimize2_cli.py`, `panel/server.py`
    (`/api/defaults`), `optimize.html`.
14. **Seeded all `@spot` spaces with WIDER exit/duration ranges** because a diagnostic showed
    every spot winner was pinned at the old scalp-sized walls (PTs to 12%, durations to
    weeks). `param_space.json` gained `@spot` variants for v7, prime, prime7, v6, scalpx,
    scalpx2, macdx, rocx.

### D. New strategy #1: MACD Crossover (`macdx` / `macdx_original`)
15. Ported Adrian's "MACD Crossover Strategy with TP, SL, Time, MACD Thresholds + Histogram
    Filter" pine into `adaptive_trader/research/macdx_engine.py`. Validated **194/219 trades
    exact** vs the 2026-07-12 XLSX export (the rest are sub-tick feed differences; this
    strategy trades ~300×/6wk with hair-trigger thresholds so feed-perfect isn't achievable).
    Reproduces pine quirks: reversal entries, rejected same-side entries with side effects,
    shared min-time gate, duration-adjusted PTs, 1-min long / 3-min short cooldowns,
    SMA-seeded EMA. Then numba-ized (2ms/full-history eval) with per-regime P matrix.
16. Fully wired: `backtest_cli.py` (run_single_macdx + run_single_macdx_opt + dispatch),
    `wf2.py` (sample_macdx, build_P_macdx, load_globals cache, eval_config branch,
    FLAT_KEYMAP, _normalize_flat ordering), `optimize2_cli.py` (choices, sampler map,
    anchor defaults, load_globals maps), `param_space.json` (macdx + macdx@spot),
    `param_names.js` (macdx_original + macdx), `ui.js` STRAT_NAMES, dropdowns in
    `optimize.html` + `backtests.html`.

### E. New strategy #2: V4 ROC/SMA (`rocx` / `rocx_original`)
17. Ported "V4 ROC[-1] 45 Closed — Leveraged ROC + SMA Trend Strategy" into
    `adaptive_trader/research/rocx_engine.py`. Long-only; entries only on new 45-min buckets
    when sampled 1-min ROC(low) is falling toward the present AND 45-min SMA slope is up; TP
    vs signal close; **pine-exact "trailing" stop that FOLLOWS the close in both directions**
    (fires only when a single close drops trailPct below the previous close — this was the
    key validation fix, went from 0/13 to 11/13 match). Validated **11/13** vs the export
    (1 sub-tick, 1 exit beyond our data). numba core, ROC/SMA lengths as variant menus,
    timeframes fixed at 1min/45min.
18. Fully wired everywhere the same way as macdx (same file list).

### F. SPOT research campaign (documented in `optimizer/EXPERIMENTS.md`)
19. Ran a multi-wave spot campaign (10k → 100k → 500k evals, 14 procs). Found the two spot
    winners; see §5. Also **fixed two campaign-critical bugs**: (a) a spot mutation leak
    where genetic mutation could restore leverage/shorts on spot candidates
    (`mutate_flat` now re-imposes spot invariants); (b) the finalize pool-scan early-stop
    ("stop after 10 survivors") degenerates on spot where nothing liquidates → it only ever
    scanned the top-10; now spot scans the full pool+reservoir. Both winners were found deep
    in the reservoir (#506, #525) exactly as the reservoir doctrine predicts.
20. rocx spot campaign: **no spot edge** — its dip-buy needs the 45-min uptrend the bear
    holdout lacks. Documented, not adopted.

### G. Live-trading execution path (the session's final and most important thread)
21. **Adopted the crash-fix `webhook_server.py`** from mexc-td-enhanced (6-hour full
    browser+driver restart to reclaim Playwright driver heap; immediate restart on
    crashed/closed page; reentrancy guards). Byte-identical copy.
22. **Decodo static residential (ISP) proxy support.** `webhook_server.py` reads
    `--proxy-server/--proxy-username/--proxy-password`, or `MEXC_PROXY_*` env, or
    `proxy_config.json` (gitignored). WebRTC/QUIC leak protection when a proxy is set.
    Startup egress-IP self-check logs a boxed `PROXY OK` / `PROXY LEAK`.
23. **Headless mode + live view.** `--headless` flag; `/screenshot` (PNG) and `/view`
    (auto-refreshing) endpoints on the executor; 1600×1000 window so the screenshot isn't
    clipped.
24. **Bandwidth savers for the metered proxy:** default page is now
    `…?type=linear_swap#info` (order panel works, chart doesn't mount → no chart kline
    stream); image/media/font requests aborted when a proxy is set (captcha/login URLs
    exempt).
25. **Control panel executor controls:** headless checkbox, green/red proxy-status
    indicator (parses the boxed log line), "View live 📷" button (proxied screenshot, works
    headless). `panel/server.py` (`/api/webhook/screenshot`, `_proxy_state`, headless
    passthrough, log truncation on start), `panel/panel.html`.
26. **Live WebSocket price feed.** `adaptive_trader/data_feed.py` gained a `LivePrice` class
    (background thread, MEXC contract WS `wss://contract.mexc.com/edge`, sub.ticker,
    auto-reconnect, own ping). `Feed.last_price()` returns the fresh tick, falling back to
    the 1m kline close if the socket is stale/down. Confirmed streaming live SOL ticks.
27. **Fast protective sub-loop in the trader.** `adaptive_trader/trader.py` now re-checks the
    protective/emergency stop against the live price every `protect_poll_seconds` (0.5s)
    between the heavier `poll_seconds` (3s) work cycles — single-threaded so it can't race
    the bar-close order logic. Config updated: `poll_seconds: 3`, `protect_poll_seconds: 0.5`.

---

## 3. KEY DECISIONS (the reasoning — most important section)

- **Two independent parameter spaces per mode, NOT per-mode fields inside one space.**
  Alternatives were (a) widen all shared ranges (lev searches then waste budget on huge
  spaces), (b) add spot/lev sub-ranges to every param (schema + UI churn). Chose the
  `<strategy>@spot` sibling approach: cleanest separation, lev spaces stay byte-identical,
  and it's the natural place to seed spot-specific wide ranges. Variant libraries stay
  shared because they drive the engine's precompute caches (per-mode libs would double the
  caches for no benefit).

- **Spot ranges seeded wide because winners were pinned at the walls.** A diagnostic scanned
  spot winners for parameters sitting at range edges; profit targets and durations were all
  pinned at the old scalp-sized caps. The optimizer can only search inside the fence, so the
  fix was to move the fence, not to run more budget. This directly answered Adrian's "I
  thought the algorithm finds the best profit ranges" — it does, but only within the walls.

- **Portfolio/combination math: averaging, not summing.** Adrian asked what combining the
  three spot winners would yield and expected 7.9+9.2+2.0 = 19.1%/mo. Explained percentages
  are per-dollar so a split-capital portfolio earns the AVERAGE (~3.2%/mo equal-weight; the
  smarter 2-way A2+E2 drop-scalpx2 blend is ~4.5%/mo with half the drawdown because
  prime↔macdx correlate only 0.16). The single-account "one big strategy" version (full
  equity per trade, single position slot, first-come) simulated to **+6.6%/mo, ×7.15,
  worst month −15.8%, ~47% DD** — more return because winners compound on the whole stack,
  but every stop is also full-size. Key teaching point: **combine by correlation, not by
  count**, and summing labels only "works" if you 3× your exposure, which is just hidden
  leverage.

- **Use OOS-best (holdout survivor) numbers, not the train-best.** When Adrian saw 9.2%/mo
  and 7.9%/mo, those were `bt best full` (train-best) lines that FAILED the holdout
  (−2.6, −3.3%/mo). The honest figures are the OOS-best configs (+5.2, +3.6%/mo). On spot
  the overfit tell is subtler than lev's LIQ-vs-no-LIQ, so the holdout line is the column to
  trust.

- **1%/day is not reachable on 1× long-only spot with these engines.** Set expectations
  honestly: 1%/day ≈ 35%/mo, above the best 10×-lev qualifier. Proved it's a SIGNAL ceiling
  not a budget one — 500k added nothing over 100k, and both attempts to engineer away the
  rare −11% stops (underwater scoring, 2-day max-hold) destroyed the edge. The meaningful
  tier (beat buy-and-hold, lower DD) was cleared; strong/stretch were not.

- **100k evals is the spot sweet spot.** 500k reproduced the same OOS-best; more budget just
  deepens in-sample fit. Matches Adrian's own intuition that ~100k "seemed" right.

- **Live execution is the app's job, not a separate webhook signal source.** Adrian thought
  he didn't need the webhook server because "the app monitors MEXC live." Clarified: the app
  DOES generate its own signals from live klines (no TradingView, no external webhooks), but
  the "webhook server" is the BROWSER-DRIVING half of the app — the only way to place orders
  since MEXC has no retail futures order API. Kept it as a separate process on purpose so the
  trader and the logged-in browser can restart independently.

- **Manual captcha login, no auto-solver.** Adrian asked if there's a good captcha solution.
  Declined to build/recommend captcha auto-solvers (a firm line, and notably mexc-td-enhanced
  itself tried solvers and removed them). Mitigation is making logins RARE: persistent Chrome
  profile + static ISP proxy keep sessions alive for weeks.

- **Static proxy = ONE endpoint, never rotate.** Decodo gives 10 ports = 10 static IPs.
  Login longevity depends on MEXC seeing one constant IP, so pick one endpoint and stick to
  it; switching ports = fresh IP = fresh captcha login. Session type must be **Static** (it
  is). Japan location confirmed allowed on MEXC (not on the prohibited-jurisdictions list;
  only app-store downloads are Japan-restricted, irrelevant to website use).

- **Belt-and-suspenders proxy application.** `launch_persistent_context(proxy=...)` silently
  ignores the proxy for Chromium in some Playwright versions, so we ALSO pass
  `--proxy-server` explicitly (with the dict only for auth), plus `--proxy-bypass-list=
  <-loopback>` so localhost trader↔executor traffic never tunnels. The actual root-cause bug
  turned out to be different (see §5), but the hardening stays.

- **WebSocket ticker for live price, not per-trade `deal` stream.** Ticker pushes every
  ~1–2s which is plenty for a protective stop; per-trade granularity was deemed unnecessary.
  Offered as a future upgrade if wanted.

- **Protective check stays single-threaded.** The fast 0.5s protective sub-loop runs on the
  main trader thread, NOT in the WebSocket callback, specifically so it can never race the
  bar-close order logic and double-submit an order. This was a deliberate safety choice over
  the marginally-faster per-tick-callback design.

- **poll_seconds 3 / protect_poll_seconds 0.5.** Heavy work (kline fetch + bar-close eval)
  only needs to notice a 3-min bar close, so 3s is ample and light. Protective stop watches
  live price at 0.5s, matching the ~1–2s ticker cadence (tighter would spin without new
  data). Honest caveat repeated to Adrian: **execution latency (Playwright button click,
  1–3s + slippage) is the real floor**, not detection latency.

- **STANDING ERROR/AUTONOMY POLICY (from earlier sessions, still in force):** Fix plumbing
  bugs in my own code autonomously, in separate commits, and report after. STOP-AND-ASK for
  anything that changes simulation semantics or touches the live trader. Never change the
  live trader autonomously. (The live-price feed was explicitly approved before building.)

---

## 4. CURRENT STATE (files + what works)

### Engines (`adaptive_trader/research/` and `research2/`)
- `research/macdx_engine.py` — MACD Crossover engine. numba `_core_macdx`, `run_macdx_P`
  (per-regime P), `run_macdx` (pine-named dict), `MACDX_DEFAULTS`, `MACDX_PNAMES`,
  `precompute_macdx`, SMA-seeded `pine_ema`. **Validated 194/219.** WORKING.
- `research/rocx_engine.py` — V4 ROC/SMA engine. numba `_core_rocx`, `run_rocx_P`,
  `run_rocx`, `ROCX_DEFAULTS`, `ROCX_PNAMES`, `ROCX_ROC_LENGTHS`/`ROCX_SMA_LENGTHS` variant
  libs, `precompute_rocx`. **Validated 11/13.** WORKING.
- `research/engine.py`, `fast_engine.py`, `research2/engine3.py`, `scalp_engine.py`,
  `optimizer2.py`, `wf2.py` — pre-existing engines/optimizer machinery, extended this
  session for macdx/rocx (samplers, builders, eval branches, load_globals cache branches).
- `research2/wf2.py` — added `sample_macdx/build_P_macdx`, `sample_rocx/build_P_rocx`,
  load_globals `macdx`/`rocx` cache branches, eval_config branches, FLAT_KEYMAP + FLAT_MENU_KEYS
  + _normalize_flat entries, and the **spot mutation-leak fix** in `mutate_flat`.

### Optimizer (`optimizer/`)
- `optimize2_cli.py` — THE unified optimizer. This session: `--stop-score`, `--resume-from`
  comma-list merge + `--merge-mode`, macdx/rocx in choices/sampler-map/anchor-defaults/
  load_globals maps, per-mode `<strategy>@spot` space resolution, **spot full-pool-scan fix**
  in finalize.
- `backtest_cli.py` — run_single dispatch. This session: `macdx_original`/`rocx_original` in
  ORIGINAL_STRATEGIES + original_defaults, run_single_macdx/run_single_macdx_opt,
  run_single_rocx/run_single_rocx_opt, `total_mult` in backtest flags, reversal reason code.
- `param_space.json` — now has `macdx`, `macdx@spot`, `rocx`, `rocx@spot`, and `@spot`
  siblings for v7/prime/prime7/v6/scalpx/scalpx2 with widened exit ranges.
- `EXPERIMENTS.md` — campaign log. Contains the earlier V7 no-liq campaign AND the new SPOT
  campaign section with wave results and the FINAL RESULT.
- `runs/` — many runs. Spot winners: `spotcamp_A2_macdx_vol3_100k`,
  `spotcamp_E2_prime_trend3_100k` (and their `_oosbest_full` backtests). Junk to consider
  deleting: `merge_merge_auto_v7_D_vol3_te0901_mh7_1m`, `__macdx_opt_test`,
  `opt2_0712_1815*` (10× "spot" fiction from the mutation leak).

### Dashboards (`dashboard/`)
- `optimize.html` — launch card (all persisted), per-mode param panel, combine-runs row,
  stop-at-score, load-setup buttons, mark-best stars, live progress bars, holdout total%.
- `backtests.html` — quick-backtest dropdown (incl. macdx_original/rocx_original), total%
  column + detail-header total, %-gain trades column, mark-best stars, strategy dropdowns.
- `assets/param_names.js` — pine-ordered param names incl. macdx/macdx_original,
  rocx/rocx_original.
- `assets/ui.js` — STRAT_NAMES incl. macdx(_original), rocx(_original).

### Panel (`panel/server.py`, `panel/panel.html`)
- Endpoints added/changed: `/api/runs2/mark`, `/api/backtests/mark`, `/api/webhook/screenshot`,
  `_proxy_state`, webhook start passes `--headless` + truncates log, `/api/webhook/status`
  returns `headless`+`proxy`, `/api/runs2` attaches `running`+`progress`, optimize2 passes
  `--stop-score`/`--merge-mode`, `/api/defaults` resolves `@spot`.
- `panel.html` — executor row now has: headless checkbox, proxy indicator, View live button +
  inline screenshot panel with auto-refresh.

### Live trading (`adaptive_trader/`)
- `trader.py` — main loop; live-price protective sub-loop added. `--live` disables dry_run.
  **UNTESTED since the loop change — parity test must be re-run before live.**
- `data_feed.py` — `LivePrice` WS class + `Feed(symbol, live=True)` + `last_price(max_age)` +
  `price_source()`. **WS confirmed streaming live SOL ticks.**
- `config.json` — `dry_run: true`, `poll_seconds: 3`, `protect_poll_seconds: 0.5`, currently
  holds an adopted v7 lev config (`adopted_from … __seed_test_genetic_2_full_improved_full`,
  at 2026-07-13 02:41). `emergency_exit_adverse: null` (no safety net — set e.g. 0.10 for a
  10%-underwater force-close).
- `webhook_server.py` — crash-fix version + proxy + headless + `/view` + `/screenshot` +
  `#info` default + bandwidth blocking + 1600×1000 window. **Proxy path proven working in
  isolation (test_proxy.py); full server run pending a restart to confirm the boxed
  PROXY OK line.**
- `proxy_config.json` — filled with Adrian's Decodo endpoint `http://isp.decodo.com:10001`
  and port-10001 credentials (country currently `jp`). GITIGNORED.
- `strategy.py` / `strategy_v6.py` / `strategy_v7.py` — live strategy adapters; parity tests
  `test_parity*.py`.

### Confirmed working
- macdx (194/219) and rocx (11/13) validations; both optimize + backtest end-to-end.
- Spot campaign runs, full-pool-scan finalize, mark-best, combine, progress bars.
- Decodo proxy via Playwright in isolation (Tokyo egress IP hiding home IP).
- Live WebSocket price streaming.

### Untested / pending
- The trader loop with the new protective sub-loop (needs parity test + dry-run).
- The real `webhook_server.py` end-to-end with proxy loaded (needs a clean restart; the
  `instance_1` Chrome profile was reset/backed up so it needs a fresh MEXC login).
- View-live screenshot at the new 1600×1000 size (needs executor restart).

### Known-good numbers
- Buy-and-hold SOL over our data: −0.9%/mo overall; holdout window (after 2025-09-01) ≈
  −13%/mo (deep bear). ANY positive holdout %/mo is real alpha.
- Spot winner A2 (macdx·vol3): +5.2%/mo full history, holdout +0.6%/mo, 20% DD.
- Spot winner E2 (prime·trend3): +3.6%/mo full, holdout +3.3%/mo, 0% DD.

---

## 5. OPEN ISSUES / THINGS THAT FAILED (don't repeat)

- **Proxy "not filled in" bug (FIXED, but instructive).** The proxy file-fallback used
  `"FILL_IN" not in str(_pc)` to detect placeholders, which stringified the WHOLE file
  including the `_readme` text that literally contains "FILL_IN" — so it ALWAYS said "not
  filled in" and ran without proxy. This is why the server leaked the home IP while
  `test_proxy.py` worked. Fixed to inspect only server/username/password fields. **Lesson:
  never scan help text for a placeholder token.**
- **Playwright persistent-context proxy quirk.** Separately, `launch_persistent_context(proxy=)`
  is known to be ignored for Chromium in some versions — mitigated by always passing
  `--proxy-server` explicitly. (The actual observed leak was the FILL_IN bug, but this
  hardening is correct and stays.)
- **Spot mutation leverage/shorts leak (FIXED).** All flat-strategy spot runs from BEFORE
  this fix are invalid — they show 10×/shorts despite being "spot" (e.g. `opt2_0712_1815`
  showed a "−63%" stop that was really −4.78% × 10× leverage). Treat any pre-fix flat spot
  run with suspicion; check the trades' lev column.
- **Spot pool-scan early-stop (FIXED).** Before the fix, spot finalize only scanned the top-10
  in-sample candidates (nothing liquidates on spot so the "10 survivors" early-stop never
  triggered a full scan). Made 100k runs look worse than 10k until re-finalized.
- **Combine-runs name truncation (FIXED).** 40-char truncation collapsed distinct merge names
  into one, silently resuming a junk run. Also stale stop-score killed merges after 1 gen.
- **rocx has no spot edge** — confirmed via full campaign, not a bug. Bull-conditional
  (needs the 45-min uptrend). Could be a lev bull-regime specialist instead; not pursued.
- **1%/day spot target is not achievable** with current engines (signal ceiling, proven).
- **macdx/rocx are NOT feed-perfect** vs TradingView (194/219, 11/13). This is expected
  (CoinAPI vs MEXC feed sub-tick differences on hair-trigger thresholds), not a bug. Don't
  chase 100%.
- **Open question: which config to take live?** Two prior no-liq qualifiers exist
  (`auto_v7_D…_oosbest_full` +13.9%/mo thin margin; `survivor2` +5.0%/mo robust). Adrian's
  `config.json` currently has a DIFFERENT adopted v7 lev config
  (`__seed_test_genetic_2_full_improved_full/holdout_best_config.json`). Confirm with Adrian
  which one he actually wants live — this is unresolved.
- **Parity test not yet run** since the trader-loop change. Must pass before `--live`.

---

## 6. NEXT STEPS (ordered, start on step 1 immediately)

1. **Restart the three processes** to load this session's code: control panel, executor
   (`webhook_server.py`), trader if running. If nothing is running, just start fresh. Order:
   panel → executor → trader.
2. **Confirm the proxy end-to-end.** Start the executor (headed, via panel or
   `python3 webhook_server.py --instance 1 --port 5001`). Look for `Proxy loaded from …`,
   `Routing browser traffic through proxy …`, and the boxed `PROXY OK — browser egress IP:
   140.99.103.105`. Load ipchicken.com / MEXC in `/view` and confirm the Tokyo IP.
3. **Do the one-time MEXC captcha login** in the executor's headed browser window (the
   `instance_1` profile was reset, so it starts logged out). Then Ctrl-C and optionally
   relaunch with `--headless` (or check the panel's headless box) — the session persists.
4. **Decide the live config with Adrian** (see §5 open question). Recommended: `survivor2`
   (robust, +5.0%/mo, worst excursion −6.3%, clears liquidation to ~14×). Ideally run its
   **walk-forward validation first** (offered but never run).
5. **Adopt the chosen config** into `adaptive_trader/config.json` (panel Adopt button keeps a
   backup) if it isn't already the one loaded.
6. **Run the parity test** for the chosen config: `python3 test_parity_v7.py` (or the matching
   test). It must show the live strategy code is byte-identical to the backtest engine for
   that candidate. Do NOT go live if it fails.
7. **Dry-run soak.** Start the trader with `dry_run: true` (current) and watch the panel/log
   for a day: confirm live feed (`price_source` = live), bar-close timing, signal sanity,
   and that `INTRABAR STOP … (live price)` fires correctly if price moves.
8. **Optionally set `emergency_exit_adverse`** (e.g. 0.10) for a safety net — the lev config
   has no per-trade stop by design.
9. **Adrian flips `--live` himself.** Never do this autonomously. Confirm proxy indicator is
   green, executor logged in, config correct, size small first.

Parallel / optional (not blocking live):
- Delete junk runs (`merge_merge_…`, `__macdx_opt_test`, `opt2_0712_1815*`).
- Build the portfolio backtest feature if Adrian wants the real 2-way/3-way combined numbers
  (a `--portfolio` mode in `backtest_cli.py`; there are unstarted tasks for it).
- Consider rocx as a lev bull-regime specialist.

---

## 6b. QUICK-START COMMANDS (run on the Mac, from repo root unless noted)

- **Control panel:** `python3 panel/server.py` → open http://127.0.0.1:8800
- **Executor (headed, for login):** `python3 webhook_server.py --instance 1 --port 5001`
- **Executor (headless + live view):** add `--headless`; view at http://127.0.0.1:5001/view
- **Proxy isolation test:** `python3 test_proxy.py` (reads `proxy_config.json`, prints egress
  IPs both launch ways — expect the Decodo IP, not the home IP).
- **Trader (dry-run):** `python3 adaptive_trader/trader.py` — **`--live` places REAL orders.**
- **Parity test (before live):** `python3 adaptive_trader/test_parity_v7.py`
- **Optimize example:** `cd optimizer && python3 optimize2_cli.py --strategy macdx --mode spot
  --method vol3 --algo genetic --procs 14 --total 100000 --train-end 2025-09-01 --name myrun`
- **Backtest a config:** `cd optimizer && python3 backtest_cli.py --config
  runs/<name>/holdout_best_config.json --name <name>_oosbest_full`
- **Re-validate a ported engine:** `python3 validation/validate_macdx.py` or
  `validation/validate_rocx.py`. **These need the TradingView XLSX exports** (were uploaded
  this session to the session uploads dir; re-upload them if re-validating). They import from
  `optimizer/` via a relative path and load segments from the cached MEXC data.
- **Optimizer README** with more flag docs: `optimizer/README.md`. Campaign log:
  `optimizer/EXPERIMENTS.md`.

## 7. CONTEXT & CONVENTIONS

- **Never touch `/Users/adrian/Code/mexc-td-enhanced`** — read-only reference project.
- **Adrian runs git himself** (Desktop Commander on his Mac) after each change-set. Do NOT
  commit/push. When work spans concerns, keep changes logically separable so he can commit in
  sensible chunks (e.g. plumbing fixes as their own commit).
- **Autonomy policy:** fix plumbing bugs in my own code autonomously + report; STOP-AND-ASK
  before anything that changes simulation semantics or touches the live trader; never change
  the live trader without approval.
- **Opt-in features, classic defaults untouched.** New optimizer options must default to the
  pre-existing behavior (this is why scoring modes, reservoir, stop-score, protective loop,
  etc. all have off/classic defaults). Adrian pushed back hard twice earlier about changes
  being made before he approved and about altering all regimes — honor "answer my question
  before making changes."
- **Max-days-in-trade is a DISQUALIFIER**, never a force-close. Adrian explicitly does not
  want positions force-closed by a max-hold rule.
- **Pine fidelity is paramount.** New strategies must be validated bar-for-bar against the
  TradingView export before being trusted. Reproduce pine quirks exactly (reversal entries,
  seeded EMAs, close-following trailing stops, cooldown timeframes).
- **Honest evaluation over impressive numbers.** Always lead with the holdout / OOS-best
  figure, not the flattering train-best/full-history number. Explain overfit tells.
- **Adrian's setup:** Mac (zsh — note it does NOT word-split unquoted vars, and `timeout` is
  not installed; use per-command invocations). Decodo static ISP proxy, endpoint
  `isp.decodo.com:10001`, currently Japan (`country-jp`). Home IP 97.120.254.99. Playwright
  1.48.0, `websockets` 16.0 available, `websocket-client` NOT installed. Panel on
  `http://127.0.0.1:8800`; executor on port 5001.
- **Tone/format Adrian prefers:** concise and direct, minimal fluff. He asks a lot of "what
  does X mean" questions — answer them plainly and completely before acting.
- **Sandbox vs Mac paths:** sandbox bash sees the repo under
  `/sessions/festive-happy-gauss/mnt/Code/strategy-lab/`; file tools use
  `/Users/adrian/Code/strategy-lab/`. Long-running processes (optimizer runs, the live
  browser) must run on the Mac via Desktop Commander, not the ephemeral sandbox.
- **Money/trades:** I never place trades, move funds, or flip the live switch. Adrian does
  all irreversible financial actions himself.

---

## 8. FILE MANIFEST (exhaustive — every file touched this session, for verification)

Generated from `git status`. Everything below is already on disk at
`/Users/adrian/Code/strategy-lab/`. Migration = `git commit` these + point the new session at
the folder (or a fresh clone).

### Source files CREATED this session (new, untracked)
- `HANDOFF.md`
- `adaptive_trader/research/macdx_engine.py`  — MACD Crossover engine
- `adaptive_trader/research/rocx_engine.py`   — V4 ROC/SMA engine
- `test_proxy.py`                             — proxy isolation test
- `validation/validate_macdx.py`              — macdx pine-validation harness
- `validation/validate_rocx.py`               — rocx pine-validation harness
- `proxy_config.json`                         — Decodo creds (GITIGNORED; excluded from git on purpose)

### Source files MODIFIED this session (tracked, not yet committed)
- `webhook_server.py`
- `adaptive_trader/trader.py`
- `adaptive_trader/data_feed.py`
- `adaptive_trader/config.json`
- `adaptive_trader/research2/wf2.py`
- `optimizer/optimize2_cli.py`
- `optimizer/backtest_cli.py`
- `optimizer/param_space.json`
- `optimizer/EXPERIMENTS.md`
- `dashboard/optimize.html`
- `dashboard/backtests.html`
- `dashboard/backtests.js`
- `dashboard/assets/param_names.js`
- `dashboard/assets/ui.js`
- `panel/server.py`
- `panel/panel.html`
- `.gitignore`

### Generated DATA this session (optimizer outputs, not source code)
Under `optimizer/runs/` — safe to keep or delete; NOT needed for the code to work:
- Spot campaign (KEEP the winners): `spotcamp_A2_macdx_vol3_100k`,
  `spotcamp_E2_prime_trend3_100k` (+ their `_oosbest_full` backtest entries in
  `dashboard/backtests.js`). Other `spotcamp_*` (A, A3, B, R1–R4) are campaign runs — keep
  for the record or prune.
- DELETE candidates (junk/invalid): `__macdx_opt_test`, `__macdx_spotguard_test`,
  `__macdx_spotspace_test`, `opt2_0712_1815*` (10× "spot" fiction from the pre-fix mutation
  leak), `merge_merge_auto_v7_D_vol3_te0901_mh7_1m*`, and `_backtest_tmp/quick_*` temp files.

### Files that legitimately live OUTSIDE the repo (do not travel via git)
- TradingView exports (`*.pine`, `*_2026-07-12.xlsx`) — were session uploads; keep locally if
  you'll ever re-validate the ported engines.
- Gitignored local runtime: `chrome_user_data/` (browser profile incl. the reset
  `instance_1.pre_proxy_*` backup), `cache/`, `proxy_config.json`.

### NOT modified — reference only
- `/Users/adrian/Code/mexc-td-enhanced` — the original source project. Read-only; we copied its
  crash-fixed `webhook_server.py` FROM it, never wrote TO it.
