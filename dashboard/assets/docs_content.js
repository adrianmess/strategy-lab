// Strategy Lab — in-app documentation. SINGLE SOURCE OF TRUTH:
// the ⓘ drawer and the Docs page both render from this file.
// Anchors are "<page>.<section>"; add a section here whenever a feature
// ships and reference it with infoBtn('<anchor>').
window.DOCS_CONTENT = {pages: [

{id:'overview', title:'Overview', intro:
 'The money dashboard: realized P&L in three windows, the cumulative and '+
 'daily charts, both accounts, every instance’s state, and the shadow '+
 'positions the routers are tracking virtually.',
 sections:[
 {id:'kpis', title:'Realized 24h / 7d / 30d', html:
  '<p>Dollar figures are exact sums of exchange-recorded closes (futures: '+
  'MEXC’s own realized P&L per position; spot: FIFO-matched fills, so '+
  'selling deposited coins counts nothing).</p>'+
  '<p>The <b>%</b> is a Modified Dietz return: window P&L ÷ the average '+
  'capital actually at risk. Deposits and withdrawals are weighted by the '+
  'fraction of the window they were present, so moving money in or out '+
  'never flatters or punishes the number.</p>'+
  '<p>Both figures follow the <b>origin switch</b> (bot / manual / both) '+
  'and the <b>account filter</b> on the cumulative card — check those two '+
  'first whenever a number looks wrong.</p>'},
 {id:'cum', title:'Cumulative realized P&L — 30d', html:
  '<p>One step per closed trade over 30 days, exchange truth. The origin '+
  'switch filters by who closed the trade (a bot event = a trader recorded '+
  'a close of that symbol on that account within ±4 minutes); the account '+
  'dropdown narrows to one account and also drives the Realized KPIs. '+
  'Under “manual only” the account dropdown hides and both accounts '+
  'show.</p>'+
  '<p>“Not enough closed trades yet” means fewer than two closes match '+
  'the current filters — usually a filter issue or a fresh P&L reset, '+
  'not missing data.</p>'},
 {id:'daily', title:'Daily P&L — last 30 days', html:
  '<p>Each bar is one day’s realized return measured on the balance that '+
  'day <i>opened</i> with — equity is walked backwards from the current '+
  'balance, subtracting each day’s realized and its deposits/withdrawals, '+
  'so early days are judged against the money they actually had.</p>'+
  '<p>The footer’s <b>TWR 30d</b> compounds the daily flow-adjusted '+
  'returns — the “skill-pure” number, immune to deposit timing, and the '+
  'right one to compare against backtests.</p>'},
 {id:'accounts', title:'Accounts', html:
  '<p>Full totals per account — spot + futures stablecoin equity across '+
  'all wallets, with per-wallet breakdown, transfer and deposit buttons. '+
  'LIVE tag = some instance is trading this account with real money.</p>'+
  '<p>The panel deliberately has <b>no withdrawal capability anywhere</b>; '+
  'withdrawals are done by hand on MEXC.</p>'},
 {id:'positions', title:'Open positions', html:
  '<p>Exchange truth from the kill-switch planner — includes anything held '+
  'on either account even if no trader is tracking it (those rows show '+
  '“untracked”).</p>'+
  '<p><b>Price Δ</b> is the trade’s price move since open — the '+
  'unleveraged P&L% (P&L% ÷ leverage). Colored by its own sign.</p>'},
 {id:'shadows', title:'Shadow positions', html:
  '<p>What each router component <i>would</i> be holding right now — '+
  'simulated only, nothing is on the exchange for them. Deep-red shadows '+
  'can be adopted (manually, via an armed rule, or by the standing '+
  'auto-adopt while the slot is flat).</p>'+
  '<p>A <b>past liq — sim only</b> tag marks zombies: virtual trades past '+
  'the point where a real position would have been liquidated. Their P&L '+
  'is not achievable and nothing will adopt or join them.</p>'}]},

{id:'instances', title:'Instances', intro:
 'One card per configured trader: state, positions, shadows, adoption '+
 'controls, performance windows and the start/stop lifecycle.',
 sections:[
 {id:'card', title:'The instance card', html:
  '<p>The state row shows RUNNING — LIVE (real money), running (dry-run) '+
  'or stopped, the config in use, and the router summary. The performance '+
  'strip shows the account’s realized 24h/7d/30d and win rate — '+
  'account-based, so manual trades on the same account are included.</p>'},
 {id:'controls', title:'Start / Stop / Restart / Pause', html:
  '<p><b>Start LIVE</b> always demands a typed confirm — real orders. '+
  '<b>Restart</b> preserves the live/dry state and re-adopts the open '+
  'position from the state file. <b>Stop</b> leaves any open position '+
  'unmanaged — the card warns you.</p>'+
  '<p><b>Pause</b> is wind-down mode: open positions keep being managed to '+
  'their normal close (mirror exits, TP/SL, overrides all still fire) but '+
  'nothing NEW opens — no fresh signals, late-joins, adoptions or cascade '+
  'slots. Applies within seconds, survives restarts. Buttons pulse while '+
  'an action is in flight.</p>'},
 {id:'hero', title:'Position banner', html:
  '<p>Each open position gets a banner: P&L% (margin-basis at leverage), '+
  'dollar P&L, the raw <b>price</b> move, entry/now/qty, the engine’s '+
  'projected close, and per-position close controls — arm a trigger '+
  'price/% or market-close now (typed confirm when live).</p>'+
  '<p>Adopted positions also show “virtual” — what the P&L would be from '+
  'the component’s original virtual entry.</p>'},
 {id:'autoadopt', title:'Adopt, armed rules & auto-adopt', html:
  '<p>Three ways to open a shadow as a real position, all flat-only: '+
  '<b>Adopt</b> (this one, now), an <b>armed rule</b> (adopt when ITS '+
  'unrealized ≤ your trigger, one-shot, survives restarts), and the '+
  'standing <b>auto-adopt</b> — whenever the slot is flat it takes the '+
  'deepest-red shadow at or below the per-pair threshold.</p>'+
  '<p>Researched presets (per config): spot = vol-scaled per-pair depths '+
  'with auto-close at +2%; lev = flat −3 all pairs, mirror exit. Guards: '+
  'anti-churn (same trade re-adopts only ≥1% below your last exit), '+
  'liquidation distance, and zombies are excluded. Enabling auto-adopt '+
  'suppresses the legacy late-join.</p>'},
 {id:'adoptclose', title:'How adopted positions close', html:
  '<p>An adopted position can have <b>two independent close triggers '+
  'racing</b>:</p>'+
  '<p><b>1 · Your auto-close</b> (auto-adopt preset, or the close % you '+
  'set on an armed rule): closes when <i>your</i> unrealized — measured '+
  'from <i>your</i> fill — reaches the target (spot preset: +2%). It '+
  'fires regardless of what the virtual trade is doing, and because you '+
  'entered at a discount it usually fires first.</p>'+
  '<p><b>2 · The mirror exit</b>: if the component’s virtual trade closes '+
  '(its own target or stop, computed from the <i>virtual</i> entry), your '+
  'real position closes with it — in either direction. Exiting on the '+
  'virtual’s stop still lands you better than the sim by your adoption '+
  'discount.</p>'+
  '<p>A manual Adopt with no close % has only trigger 2 — you ride the '+
  'component’s full plan. Note the banner’s <b>projected close</b> '+
  'describes the VIRTUAL trade’s plan (anchored at the virtual entry), '+
  'not your +% target — the “auto-close ≥ +x%” tag is yours.</p>'},
 {id:'cascade', title:'Cascade (capital pool)', html:
  '<p>With <code>"cascade": true</code> the one-slot router becomes a '+
  'capital pool: every fresh signal opens while free capital remains, one '+
  'position per symbol, each entry capped by the liquidity guardrail. '+
  'Validated in simulation 2026-08; matters once depth caps bind '+
  '(lev ~$2–3k equity, spot ~$5k). The card shows CASCADE and the free '+
  'capital; dry-run instances track a paper capital pool.</p>'}]},

{id:'trade', title:'Trade', intro:
 'Manual trading on either account, spot or leverage — the same wallets '+
 'the bots trade, with MEXC-style position management.',
 sections:[
 {id:'chart', title:'Chart', html:
  '<p>TradingView-style candles with EMA 5/10/30/60, MACD, RSI and %B '+
  'panes (toggles persist), DST-aware timezone picker, and pan/zoom that '+
  'survives the 12s refresh. The selected pair persists too. Spot-only '+
  'tokens chart from spot candles automatically.</p>'},
 {id:'order', title:'Order panel', html:
  '<p>Both accounts × Leverage/Spot with leverage presets, margin max, '+
  'limit price, live per-pair fee line and preflight balance checks. Every '+
  'order needs the typed TRADE confirm.</p>'},
 {id:'tabs', title:'Positions / history tabs', html:
  '<p>MEXC-style tabs: Positions (manual only — strategy positions live '+
  'in Bot positions below), Open Orders, Position History, Order & Trade '+
  'History, Assets. Per-row: Flash Close, TP/SL (margin-basis % for '+
  'futures, price-move % for spot, entire or partial), close at price or '+
  'market, and on-close wallet transfers — armed rules render as tags '+
  'with computed trigger prices.</p>'},
 {id:'botpos', title:'Bot positions', html:
  '<p>Each running instance’s REAL positions with source tags: BOT SIGNAL, '+
  'SHADOW ADOPTED / AUTO (purple), SHADOW LATE-JOIN (orange). Shows market '+
  'entry, now, price Δ, uPnL, the engine’s projected close and any '+
  'armed auto-close. Closing these changes the STRATEGY’s position.</p>'}]},

{id:'risk', title:'Risk', intro:
 'Exposure, liquidation distance, and the standing risk rules including '+
 'the live liquidity guardrail.',
 sections:[
 {id:'exposure', title:'Exposure & liquidation table', html:
  '<p>Gross notional, committed margin and the nearest liquidation '+
  'distance across every tracked position, plus a per-position table with '+
  'price Δ and liq distance (≈ 1/leverage from entry, the engine’s own '+
  'model).</p>'},
 {id:'guard', title:'Liquidity guardrail', html:
  '<p>Every API entry (futures margin, spot spend) is capped at a fraction '+
  '(default 25%) of the worst-side order-book depth within N bps (default '+
  '5) of mid, read live at order time — so a signal can’t push a thin '+
  'book. Thin pairs bind first; majors effectively never. Tunable here '+
  'without restarts; depth-read failures fail OPEN so a depth outage '+
  'cannot halt trading. Per-config overrides exist.</p>'}]},

{id:'history', title:'History', intro:
 'Exchange-truth closed trades for all four account×market combos.',
 sections:[
 {id:'origin', title:'Origin column & filters', html:
  '<p>Each close is labeled BOT (a trader recorded a close of that symbol '+
  'on that account within ±4 min) or MANUAL. Caveat: bot trades closed '+
  'while a trader was down read as manual. The account filter includes '+
  'combos no instance trades.</p>'+
  '<p>BOT closes show <b>via &lt;component&gt;</b> under the symbol — the '+
  'exact strategy that ran the trade. Click it to open that component’s '+
  'backtest entry on the classic Backtests page (the OOS-best or '+
  'train-best candidate it actually trades).</p>'},
 {id:'exclude', title:'Exclude / Restore', html:
  '<p>Excluded trades stay listed but leave every aggregate on every page '+
  '— for one-off distortions you don’t want polluting the KPIs. '+
  'Reversible per trade.</p>'},
 {id:'reset', title:'P&L reset (fresh start)', html:
  '<p>A per-account reset epoch hides all P&L, flows and history before '+
  'the reset moment from every view — the balance at reset becomes the '+
  'new baseline. The exchange keeps everything; a reset is fully undoable '+
  'via the API and the KPIs show “fresh since” while one is active. '+
  'Useful after profit extractions.</p>'}]},

{id:'backtests', title:'Backtests', intro:
 'The published results store — 31k+ entries — with re-runs, risk '+
 'classification and liquidation history. (The classic page at / has the '+
 'full research tooling; this page is the quick index.)',
 sections:[
 {id:'table', title:'Reading the table', html:
  '<p>Growth/mo, total multiple, max drawdown, trades, win rate. Pills: '+
  'leverage (e.g. 10x) and stop-loss class — green <b>SL</b> (stops in '+
  'every regime), amber <b>partial SL</b> (some regimes only), red '+
  '<b>stopless</b> (leverage with no stop — liquidation is the only '+
  'floor). <b>LIQ</b> = the account liquidated in this sim; <b>was LIQ</b> '+
  '= a previous run of this exact entry liquidated — that flag is sticky '+
  'forever, because a config that died on any data vintage is fragile.</p>'},
 {id:'rerun', title:'Re-running on current data', html:
  '<p>Re-runs replace the entry and re-stamp its created date (green when '+
  '&lt;24h). Bulk options on the classic page: checkbox column for '+
  'hand-picked batches, per-router re-runs, and “re-run TRADED '+
  'strategies” which reads the exact candidates your running instances '+
  'use. A progress bar tracks the batch and the page refreshes itself '+
  'once results land.</p>'+
  '<p>A re-run is <i>today’s engine + today’s fees + today’s data</i> — '+
  'results can legitimately change from the original run.</p>'},
 {id:'candidates', title:'OOS-best vs train-best', html:
  '<p>Each optimizer run publishes two candidates: <b>_oosbest_full</b> '+
  '(won the out-of-sample holdout — what routers trade) and <b>_full</b> '+
  '(won on training data). They are different parameter sets and can '+
  'disagree badly — one liquidating while the other is clean is the '+
  'holdout system working. Router components show which candidate they '+
  'trade.</p>'}]},

{id:'concepts', title:'Concepts', intro:
 'Cross-cutting ideas the pages rely on.',
 sections:[
 {id:'dietz', title:'Modified Dietz & TWR', html:
  '<p><b>Modified Dietz</b>: return on the average capital at risk — '+
  'window P&L ÷ (start balance + each flow × the fraction of the window '+
  'it was present). Fair when money moves mid-window.</p>'+
  '<p><b>TWR</b>: daily flow-adjusted returns compounded — removes flow '+
  'timing entirely. Dietz answers “what did my money earn”, TWR answers '+
  '“how well did the strategy trade”.</p>'},
 {id:'router', title:'FCFS router', html:
  '<p>N component strategies (pair × timeframe × family × method) run '+
  'virtually, engine-exact. When the slot is free, the first fresh signal '+
  'opens the real position; ties break by component order, matching the '+
  'backtest. The real position mirrors its component’s virtual exit. If '+
  'the SIM liquidates, the real position is NOT closed — it detaches and '+
  'is managed standalone: it closes only when it recovers to the '+
  'standalone take-profit (+0.5% by default). Leveraged standalone '+
  'positions are never closed at a loss by the panel — only a strategy’s '+
  'own stop-loss realizes leveraged losses, and liquidation is left to '+
  'the exchange (its real liquidation price sits above the naive 1/lev '+
  'threshold model). Spot standalones keep a −50% disaster brake, since '+
  'spot has no exchange liquidation to backstop them.</p>'},
 {id:'safety', title:'Safety model', html:
  '<p>Live orders only ever start from a typed confirm. The panel cannot '+
  'withdraw funds. MEXC private API goes through the proxy pool with '+
  'IP-whitelisted keys; market data goes direct. Panel restarts are safe: '+
  'traders survive and re-adopt with their live flag; machine reboots '+
  'resume traders in their previous state.</p>'}]}
]};
