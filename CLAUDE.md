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
  API under `/api/...`). Started by cron watchdog
  (`scripts/panel_watchdog.sh`: `@reboot` + every minute; panel process
  pattern is `" server\.py$"` — the venv python shows as framework Python).
- Panel restarts are SAFE (running traders survive and are re-adopted with
  their live flag); machine reboots resume traders in their previous
  live/dry state (persisted in `panel/instances.json`).
- This MacBook's repo (`~/Code/strategy-lab`) is the DEV copy: edit here,
  syntax-check, `scp` to the mini, commit+push (for pushes:
  `SSH_AUTH_SOCK=$(launchctl getenv SSH_AUTH_SOCK) git push`).

## Hard rules

- NEVER flip a trader to live and never place/close orders — Adrian does that
  himself in the panel (confirm-LIVE). Dry-run starts/restarts are fine.
- NEVER restart a RUNNING trader without asking — check its live flag first
  (`/api/instances` → `trader_live`); a plain restart silently downgrades
  live→dry (this bit us once).
- Don't override pause states on workers — they may be deliberate.
- MEXC private API only via the Decodo proxy pool
  (`adaptive_trader/proxy_pool.json`, per-account ports); klines REST and
  WebSocket go DIRECT. Keys are IP-whitelisted to the proxy IPs.

## Quick orientation

- Instances/trader configs: `adaptive_trader/config*.json` (each has its own
  state/log; `execution:"api"`, `api_account: mexc1|mexc2`).
- Optimizer runs: `optimizer/runs/` (~27k dirs); campaigns in
  `optimizer/campaigns/`; gamut worker budget via `optimizer/gamut_limits.json`
  + `scripts/gamut_ctl.sh` (status|pause|resume|cores N — per-PID signals only,
  NEVER process groups).
- Published backtests: `dashboard/backtests.js` (append via flock; entries
  named `<run>_full`, `<run>_oosbest_full`, routers `*_fcfs_full/_fcfs_wf`).
- Market data refresh must go through
  `adaptive_trader/research/update_data.py` (it clears engine caches — stale
  caches silently simulate old windows).
- AWS EC2 fleet: torn down 2026-08-15, nothing billable remains. Rebuild
  guide: `docs/EC2_OFFLOAD_RUNBOOK.md`.
