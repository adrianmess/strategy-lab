# Hermes integration guide — Strategy Lab notifications

You are reading this because Adrian wants you (Hermes) to monitor his
automated crypto-trading system ("Strategy Lab") and notify him on WhatsApp
when things happen. This file tells you what the system is, what events it
emits, how to receive them, and what to do with them.

## What Strategy Lab is (30 seconds)

A self-hosted platform on Adrian's Mac that trades SOL/USDT on MEXC using
validated strategies ("routers" that switch between component strategies by
market regime). It runs one or more **trader instances** — each with its own
config file, either **dry-run** (signals logged, no real orders) or **LIVE**
(real money). Execution goes through the MEXC API (accounts `mexc1`, later
`mexc2`) or a browser-automation fallback. A local control panel runs at
`http://127.0.0.1:8800` on the Mac.

## How to receive events

Every event is appended, one JSON object per line, to:

```
/Users/adrian/Code/strategy-lab/adaptive_trader/notifications.log
```

This file is **always written** — tailing it is the zero-config way to
subscribe. Alternatively, two push modes exist in
`adaptive_trader/notify_config.json`: `webhook_url` (events arrive as HTTP
POST, JSON body, optional `Authorization: Bearer <token>`) and `command`
(a shell command run with the event JSON on stdin). Use whichever suits you;
the payloads are identical.

## Event schema

Every payload has:

```json
{ "source": "strategy-lab", "event": "<type>", "at": "YYYY-MM-DD HH:MM:SS", ... }
```

Common fields: `account` (e.g. `"mexc1/api"` = account/execution-path),
`config` (which trader instance's config file, e.g.
`config_camp_c4_m_spot_vol3.json`), `live` (**true = real money**,
false = dry-run rehearsal).

### Event types

| event | meaning | extra fields |
|---|---|---|
| `position_opened` | a trade was opened | `symbol`, `side` (LONG/SHORT), `qty`, `lev`, `price` |
| `position_closed` | a trade was closed | `symbol`, `reason`, `price`, `result`, sometimes `position` |
| `order_failed` | an order could not be placed/closed (includes API/proxy failures on the API path) | `action` (open/close), `detail` |
| `trader_error` | a trader's main loop threw an exception | `detail` |
| `proxy_alert` | the browser executor's proxy leaked or failed its check | `component`, `detail` |
| `test` | connectivity test, sent manually | `note` |

`order_failed`, `trader_error`, `proxy_alert` are rate-limited at the source
(max one per source per 30 min), so each one you see is meaningful.

Close reasons you may encounter: `profit_target`, `stop_loss`,
`router:virtual_exit` (a router component's strategy exited),
`emergency_exit` (safety net fired — treat as important), `liquidation`
(should never happen — treat as critical).

## Notification policy (Adrian's standing preference)

- **Always message Adrian on WhatsApp** for: any event with `"live": true`;
  any `order_failed`, `trader_error`, or `proxy_alert` (regardless of live
  flag); any close with reason `emergency_exit` or `liquidation`.
- **Stay silent** (or batch into a daily digest at most) for dry-run
  (`"live": false`) position events — they are rehearsals.
- Format messages briefly, e.g.:
  `📈 mexc1 LIVE: opened LONG 5.2 SOL @ 77.31 (3x) — spot router` or
  `⚠️ mexc1: order FAILED (open) — <detail>`.
  Include the account and whether it was live. Adrian may refine this policy
  by telling you directly; his word overrides this file.

## Getting context (read-only, optional)

If you can reach the Mac, the control panel exposes read-only JSON:

- `GET http://127.0.0.1:8800/api/mexc/account` — balances and open positions
  per account (the exchange's ground truth).
- `GET http://127.0.0.1:8800/api/status?instance=2` — a trader instance's
  state (running, live flag, config, position, recent log lines).
- `GET http://127.0.0.1:8800/api/processes` — every trading-related process,
  with warnings if duplicates exist.

Useful when Adrian asks follow-ups like "what's my balance?" after an alert.

## Hard rules for you

1. **Never place, modify, or close trades**, move funds, or call any
   non-GET endpoint. You are a messenger and a reader, nothing more.
2. **Never kill processes or edit files** in the strategy-lab folder,
   including this system's configs — even if an alert looks urgent. Report
   to Adrian; he acts, or his coding assistant does.
3. Do not forward API keys, tokens, or file contents from this system to
   anyone or anywhere except Adrian's own WhatsApp.
4. If events stop arriving entirely for >24h while Adrian believes trading
   is live, that silence is itself worth one message ("no events in 24h —
   is the notifier or trader down?").
