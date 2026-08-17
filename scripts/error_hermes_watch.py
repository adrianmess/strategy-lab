#!/usr/bin/env python3
"""Cron (every minute, on the mini): forward NEW error events from the
trader notifications.log to Adrian via Hermes (WhatsApp DM, `hermes send`).
Offset-tracked so each error is sent exactly once; batches per run."""
import json
import os
import subprocess

NL = os.path.expanduser("~/strategy-lab/adaptive_trader/notifications.log")
ST = os.path.expanduser("~/strategy-lab/.err_notify_offset")
HERMES = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes")

off = 0
try:
    off = int(open(ST).read().strip())
except Exception:
    pass
try:
    size = os.path.getsize(NL)
except OSError:
    raise SystemExit
if size < off:
    off = 0                      # log rotated/truncated — start over
if size == off:
    raise SystemExit
with open(NL) as f:
    f.seek(off)
    chunk = f.read()
    new_off = f.tell()
open(ST, "w").write(str(new_off))

errs = []
for ln in chunk.splitlines():
    try:
        e = json.loads(ln)
    except Exception:
        continue
    ev = e.get("event") or ""
    if "fail" in ev or "error" in ev:
        errs.append(f"[{e.get('at', '?')}] {e.get('config', '?')} "
                    f"{ev}{' (' + e['action'] + ')' if e.get('action') else ''}"
                    f": {e.get('detail') or e.get('reason') or ''}")
if not errs:
    raise SystemExit

body = "\n".join(errs[:10])
if len(errs) > 10:
    body += f"\n(+{len(errs) - 10} more)"
env = {**os.environ, "HERMES_HOME": os.path.expanduser("~/.hermes")}
subprocess.run([HERMES, "send", "-q", "-t", "whatsapp",
                "-s", "⚠️ strategy-lab API error", body],
               env=env, timeout=90)
