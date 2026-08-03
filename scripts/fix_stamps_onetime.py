"""One-time: shift UTC-stamped 'created' fields (from the EC2 pre-TZ-fix era)
back 7h so date sorting is truthful. Window: 2026-08-03 03:00..05:45 'UTC as
written' — no legitimate PDT entry exists in that wall-clock window."""
import json, os, sys, fcntl
import datetime as dt
P = sys.argv[1]
LO, HI = "2026-08-03 03:00", "2026-08-03 05:46"
with open(P + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    txt = open(P).read()
    body = txt[txt.index("=") + 1:].lstrip()
    entries, _ = json.JSONDecoder().raw_decode(body), None
    entries = entries[0]
    n = 0
    for e in entries:
        c = e.get("created") or ""
        if e.get("name", "").startswith("g0801_2122") and LO <= c < HI:
            t = dt.datetime.strptime(c, "%Y-%m-%d %H:%M") - dt.timedelta(hours=7)
            e["created"] = t.strftime("%Y-%m-%d %H:%M")
            n += 1
    tmp = P + ".tmpfix"
    with open(tmp, "w") as f:
        f.write("window.BACKTESTS = "); json.dump(entries, f); f.write(";")
    os.replace(tmp, P)
print(f"shifted {n} entries")
