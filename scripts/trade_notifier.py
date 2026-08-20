#!/usr/bin/env python3
"""macOS banner + sound when a trade opens or closes.

Runs on the MAC YOU SIT AT (the MacBook), not the mini — a notification on a
headless machine's screen helps nobody. It polls the panel's trade feed and
fires a native notification for anything new.

Why not the browser's Notification API: the panel is served over plain HTTP on
a .local hostname, which Chrome treats as an insecure context, so
Notification.permission is "denied" before you can even ask. Native beats
fighting that, and it keeps working when no tab is open.

  python3 scripts/trade_notifier.py                 # foreground, ctrl-C to stop
  python3 scripts/trade_notifier.py --once          # one poll, for testing
  python3 scripts/trade_notifier.py --test          # fire a sample notification
  python3 scripts/trade_notifier.py --install       # run at login via launchd

Sound names are the ones in /System/Library/Sounds (Glass, Ping, Submarine...).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATE = os.path.expanduser("~/.strategy_lab_notifier.json")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.strategylab.notifier.plist")
DEFAULT_HOST = "http://admns-Mac-mini.local:8800"


def _key():
    """The panel key. Read it over SSH once and cache it — the file lives on
    the mini, this script runs on the laptop."""
    env = os.environ.get("PANEL_KEY")
    if env:
        return env
    try:
        return json.load(open(STATE)).get("key") or ""
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ssh", "-i", os.path.expanduser("~/.ssh/lab_auto_ed25519"),
             "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "admn@admns-Mac-mini.local",
             "cat strategy-lab/panel/panel_key.json"],
            capture_output=True, text=True, timeout=25)
        k = json.loads(out.stdout).get("key")
        if k:
            _save({"key": k})
            return k
    except Exception as e:
        print(f"could not read the panel key: {e}", file=sys.stderr)
    return ""


def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save(patch):
    d = _load()
    d.update(patch)
    try:
        json.dump(d, open(STATE, "w"))
        os.chmod(STATE, 0o600)      # it caches the panel key
    except Exception:
        pass


def notify(title, message, subtitle="", sound="Glass"):
    """Native banner. osascript is always present; no dependency to install."""
    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{esc(message)}" '
              f'with title "{esc(title)}" '
              f'subtitle "{esc(subtitle)}" '
              f'sound name "{esc(sound)}"')
    try:
        subprocess.run(["osascript", "-e", script], timeout=15,
                       capture_output=True)
        return True
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)
        return False


def fetch(host, key, limit=25):
    url = f"{host}/api/trades?limit={limit}"
    req = urllib.request.Request(url, headers={"X-Panel-Key": key})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("trades") or []


def describe(t):
    """One line a human can act on, without opening anything."""
    ev = (t.get("event") or "").upper()
    sym = t.get("symbol") or ""
    cfg = os.path.basename(t.get("config") or "")
    inst = cfg.replace("config_fcfs_", "").replace(".json", "")
    live = "LIVE" if t.get("live") else "dry"
    lev = t.get("lev")
    side = "" if not lev or lev == 1 else f" {lev:.0f}x"
    if ev == "CLOSE":
        pct = t.get("pct")
        net = t.get("net")
        bits = []
        if pct is not None:
            bits.append(f"{pct:+.2f}%")
        if net is not None:
            bits.append(f"{net:+.2f} USDT")
        res = " · ".join(bits) if bits else "closed"
        title = f"{'📈' if (t.get('pct') or 0) >= 0 else '📉'} {sym} closed {res}"
        sound = "Glass" if (t.get("pct") or 0) >= 0 else "Basso"
    else:
        px = t.get("price")
        title = f"▶ {sym} opened{side}" + (f" @ {px}" if px else "")
        sound = "Ping"
    return title, f"{inst} · {live}", (t.get("comp") or ""), sound


def poll_once(host, key, seen, announce=True):
    try:
        trades = fetch(host, key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("panel refused the key — set PANEL_KEY or re-read it",
                  file=sys.stderr)
        return seen, 0
    except Exception:
        return seen, 0        # panel asleep / laptop off the network
    fired = 0
    fresh = []
    for t in trades:
        tid = f"{t.get('at')}|{t.get('event')}|{t.get('symbol')}|{t.get('comp')}"
        if tid in seen:
            continue
        fresh.append((tid, t))
    # oldest first so a burst arrives in the order it happened
    for tid, t in sorted(fresh, key=lambda x: x[1].get("at") or ""):
        seen.add(tid)
        if announce:
            title, sub, msg, sound = describe(t)
            notify(title, msg or sub, sub, sound)
            fired += 1
    return seen, fired


def install():
    py = sys.executable
    script = os.path.join(HERE, "trade_notifier.py")
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.strategylab.notifier</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/strategylab_notifier.err</string>
  <key>StandardOutPath</key><string>/tmp/strategylab_notifier.out</string>
</dict></plist>
"""
    open(PLIST, "w").write(plist)
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    r = subprocess.run(["launchctl", "load", PLIST], capture_output=True,
                       text=True)
    print(f"installed {PLIST}")
    print("it now starts at login and restarts if it dies")
    if r.returncode:
        print(r.stderr.strip())
    print("stop it with:  launchctl unload " + PLIST)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PANEL_HOST_URL",
                                                     DEFAULT_HOST))
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--install", action="store_true")
    a = ap.parse_args()

    if a.install:
        install()
        return 0
    if a.test:
        ok = notify("📈 SUI_USDT closed +2.86% · +55.02 USDT",
                    "#2 DOGE_USDT/3m·macdx", "All_pairs_SPOT · LIVE", "Glass")
        print("sent" if ok else "failed")
        return 0 if ok else 1

    key = _key()
    if not key:
        print("no panel key — export PANEL_KEY=... and retry", file=sys.stderr)
        return 1
    # first poll only BASELINES: never dump the backlog into your face
    seen = set()
    seen, _ = poll_once(a.host, key, seen, announce=False)
    print(f"watching {a.host} — {len(seen)} existing events baselined")
    if a.once:
        return 0
    while True:
        time.sleep(a.interval)
        seen, n = poll_once(a.host, key, seen)
        if n:
            print(f"{time.strftime('%H:%M:%S')} fired {n} notification(s)",
                  flush=True)
        if len(seen) > 4000:
            seen = set(list(seen)[-2000:])


if __name__ == "__main__":
    sys.exit(main())
