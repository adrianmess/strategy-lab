# Strategy Lab — session context

Personal crypto research/trading platform (optimize → backtest → live-trade on
MEXC). Deep history and current campaign state live in `HANDOFF.md` — read it
for anything non-trivial. This file is the always-loaded operational context.

## The Mac mini is the hub

The application (control panel, live/dry traders, data, 48GB state) runs on the
Mac mini, NOT on this MacBook. To act on it, SSH (passphrase-less automation
key, works non-interactively):

```
ssh -i ~/.ssh/lab_auto_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR admn@admns-Mac-mini.local '<command>'
```

- Repo on the mini: `~/strategy-lab` (user `admn`, macOS, python at
  `~/venv/bin/python3`). zsh quoting: write ssh options inline, never in a
  variable; avoid `$IP:` history-expansion pitfalls.
- Control panel: `http://admns-Mac-mini.local:8800` (serves dashboard pages;
  API under `/api/...`). **Needs a key from off-box**: append `?k=<key>` once
  (cookie is then remembered); key lives in `panel/panel_key.json` on the
  mini. Requests from 127.0.0.1 are exempt, so SSH + `curl 127.0.0.1:8800`
  keeps working unchanged. Cross-origin POSTs are refused outright.
  The redesigned UI is at `/terminal`; the classic panel stays at `/`.
  Started by cron watchdog
  (`scripts/panel_watchdog.sh`: `@reboot` + every minute; panel process
  pattern is `" server\.py$"` — the venv python shows as framework Python).
- Panel restarts are SAFE (running traders survive and are re-adopted with
  their live flag); machine reboots resume traders in their previous
  live/dry state (persisted in `panel/instances.json`).
- This MacBook's repo (`~/Code/strategy-lab`) is the DEV copy: edit here,
  syntax-check, `scp` to the mini, commit+push (for pushes:
  `SSH_AUTH_SOCK=$(launchctl getenv SSH_AUTH_SOCK) git push`).

## Fees — read before trusting any number

MEXC prices **API trading separately from the website**, and the API schedule
overrides web rates, 0-fee promos and MX discounts:

| per side | maker | taker | taker round trip |
| --- | --- | --- | --- |
| futures API | 0.06% | 0.08% | 0.160% of notional |
| spot API | 0.00% | 0.05% | 0.100% of notional |

- `contract/detail` reports the **web** rates (it advertises 0% maker) — it is
  the wrong source for anything we execute. The panel keeps them as
  `web_maker`/`web_taker` and floors the real ones at the API schedule.
- `adaptive_trader/fees.json` is the source of truth (panel-refreshed hourly,
  reconciled against OBSERVED per-fill rates from `order_deals`, both sides).
  Everything reads it through `research2/fees_live.per_side(mode, coin, side)`.
  `LAB_FEE_OVERRIDE` (fraction/side) wins over all of it; `LAB_FEE_SIDE`
  picks maker vs taker.
- Off-box workers must NOT rely on a local `fees.json` — the MacBook's was
  three weeks stale and still held the web 0%. Shards carry the rate.
- **Anything published before 2026-09-18 was costed wrong** (advertised 0-2bp
  instead of 8bp), and v7 backtests ignored fees entirely. The corpus was
  re-costed 2026-09-18; entries carry `fee_per_side`/`fee_side`, and
  `fee_alt` holds the same run at the other side of the book. A missing
  `fee_per_side` means stale — re-run it before believing it.
- Funding is NOT modelled anywhere. Immaterial on scalps, real on multi-day
  holds.

## Hard rules

- NEVER flip a trader to live and never place/close orders — Adrian does that
  himself in the panel (confirm-LIVE). Dry-run starts/restarts are fine.
- NEVER restart a RUNNING trader without asking — check its live flag first
  (`/api/instances` → `trader_live`); a plain restart silently downgrades
  live→dry (this bit us once).
- Don't override pause states on workers — they may be deliberate.
- ALWAYS call an instance by its panel NAME ("MEX Lev 1", "MEX2 Spot"), never
  by its numeric id — the ids appear nowhere in the UI, so "instance 4" means
  nothing to Adrian. Look names up in `/api/instances` (`name` field) before
  writing about one. Same rule in code: user-facing strings use `_iname(i)`.
- MEXC private API only via the Decodo proxy pool
  (`adaptive_trader/proxy_pool.json`, per-account ports); klines REST and
  WebSocket go DIRECT. Keys are IP-whitelisted to the proxy IPs. Ports
  **10005 and 10007 are European and ~450ms slower** than the other eight —
  avoid. Browser executor pins `browser_port` (10009).
- NO browser automation against MEXC. Their Risk Control Guideline 5.2 bars
  unauthorized automated order placement, and it is not an API-vs-browser
  distinction — a script clicking the site is covered. Decided 2026-09-18;
  `webhook_server.py` stays in the repo but is not to be used for trading.
- A pending order is never abandoned without proof it is dead. Fills settle
  asynchronously: re-read before concluding "gone", and if a cancel fails,
  keep tracking it. (2026-09-17: a post-only entry was declared gone 7s after
  placing, filled anyway, and left 6,363 SUI untracked.)

## Quick orientation

- Instances/trader configs: `adaptive_trader/config*.json` (each has its own
  state/log; `execution:"api"`, `api_account: mexc1|mexc2`).
- Optimizer runs: `optimizer/runs/` (~27k dirs); campaigns in
  `optimizer/campaigns/`; gamut worker budget via `optimizer/gamut_limits.json`
  + `scripts/gamut_ctl.sh` (status|pause|resume|cores N — per-PID signals only,
  NEVER process groups).
- Gamut machine assignment (Adrian, 2026-09-11): the MacBook runs ONE
  campaign at a time (agent-enforced), and should be given pairs that are
  NOT already being gamutted on other machines (EC2) — only split a pair
  across machines when there aren't enough uncovered pairs to go around.
  Pairs need real history before gamutting (dgai/pons were too young
  2026-09; check the data range first).
- Published backtests: `dashboard/backtests.js` (append via flock; entries
  named `<run>_full`, `<run>_oosbest_full`, routers `*_fcfs_full/_fcfs_wf`).
- Market data refresh must go through
  `adaptive_trader/research/update_data.py` (it clears engine caches — stale
  caches silently simulate old windows).
- AWS EC2 fleet: torn down 2026-09-16 after gamut_gspot_newpairs +
  gamut_gorig_mh12 completed (results verified on the mini first). Nothing
  billable remains — only the free `gamut-ssh` SG and `gamut-key` key pair
  were kept for the next rebuild. Rebuild guide: `docs/EC2_OFFLOAD_RUNBOOK.md`.
