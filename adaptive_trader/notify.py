#!/usr/bin/env python3
"""Event notifications -> OpenClaw/Hermes webhook (-> WhatsApp).

The trading system EMITS structured events; your agent owns formatting and
delivery. Configure adaptive_trader/notify_config.json (gitignored):

Three delivery modes — use whichever your agent (Hermes) can ingest;
any combination may be enabled at once:
  { "enabled": true,
    "webhook_url": "http://host:port/hooks/strategy-lab",   # HTTP POST (JSON)
    "token": "optional-bearer-token",
    "command": "hermes notify --stdin" }                    # shell cmd, JSON on stdin
Third option needs no config at all: notifications.log is ALWAYS written
(newline-delimited JSON) — an agent with file access can simply tail it.

Design rules:
  - fire-and-forget, 5s timeout, NEVER raises into the trading loop;
  - every event is ALSO appended to notifications.log (works with no webhook);
  - repeated error events are rate-limited (one per event-key per 30 min) so a
    crash loop can't flood WhatsApp;
  - position events are never rate-limited.

Payload contract (what Hermes receives):
  { "source": "strategy-lab", "event": "position_opened" | "position_closed" |
    "order_failed" | "trader_error" | "proxy_alert" | "test",
    "at": "2026-07-21 22:41:03", ...event fields... }

Test:  python3 notify.py --test
"""
import json, logging, os, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "notify_config.json")
LOG_PATH = os.path.join(HERE, "notifications.log")
RATE_LIMIT_S = 30 * 60
_last_sent = {}
_lock = threading.Lock()
logger = logging.getLogger("notify")

_RATE_LIMITED_EVENTS = {"trader_error", "proxy_alert", "order_failed"}


def _config():
    try:
        return json.load(open(CFG_PATH))
    except Exception:
        return {}


def notify(event, **fields):
    """Emit an event. Safe to call from anywhere in the trading loop."""
    payload = dict(source="strategy-lab", event=event,
                   at=time.strftime("%Y-%m-%d %H:%M:%S"), **fields)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass
    cfg = _config()
    if not cfg.get("enabled") or not (cfg.get("webhook_url")
                                      or cfg.get("command")):
        return False
    if event in _RATE_LIMITED_EVENTS:
        key = (event, fields.get("config") or fields.get("account") or "")
        with _lock:
            if time.time() - _last_sent.get(key, 0) < RATE_LIMIT_S:
                return False
            _last_sent[key] = time.time()
    def _send():
        if cfg.get("webhook_url"):
            try:
                import requests
                headers = {"Content-Type": "application/json"}
                if cfg.get("token"):
                    headers["Authorization"] = f"Bearer {cfg['token']}"
                requests.post(cfg["webhook_url"], json=payload,
                              headers=headers, timeout=5)
            except Exception as e:
                logger.warning("notify webhook failed (%s): %s", event, e)
        if cfg.get("command"):
            try:
                import subprocess
                subprocess.run(cfg["command"], shell=True,
                               input=json.dumps(payload).encode(),
                               timeout=15, capture_output=True)
            except Exception as e:
                logger.warning("notify command failed (%s): %s", event, e)
    threading.Thread(target=_send, daemon=True).start()
    return True


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        ok = notify("test", note="strategy-lab notification test",
                    account="mexc1")
        print("config:", json.dumps(_config(), indent=1) or "(none)")
        print("dispatched:", ok, "— check notifications.log and your "
              "OpenClaw hook / WhatsApp")
        time.sleep(6)   # let the sender thread finish before exit
    else:
        print(__doc__)
