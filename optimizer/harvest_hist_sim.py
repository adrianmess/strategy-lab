#!/usr/bin/env python3
"""One-off research sim: would a harvest overlay (take profit / re-enter on
dips while a component's virtual trade is deep red) have beaten the current
late-join-and-mirror-out behavior across a component's FULL history?

Reads the component's published backtest trades + 1m closes; for every long
virtual trade that went >= RED red, adopts at the first red bar and either
(a) holds to the virtual exit (current behavior) or (b) cycles TP/DIP.
Spot only, taker fee both sides, one position at a time — same exposure.
"""
import json
import sys
import pandas as pd

NAME = sys.argv[1] if len(sys.argv) > 1 else \
    "sp1w_xrp1m_v7_volXtrend9_hAlt30_cls_oosbest_full"
PARQ = sys.argv[2] if len(sys.argv) > 2 else \
    "/Users/admn/strategy-lab/adaptive_trader/research/data/xrp_spot_1min.parquet"
FEE = 0.0005
TP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01
DIP = float(sys.argv[4]) if len(sys.argv) > 4 else 0.01
RED = 0.01

want = ('"name": "%s"' % NAME).encode()
with open("/Users/admn/strategy-lab/dashboard/backtests.js", "rb") as f:
    data = f.read()
if data.find(want) < 0:
    sys.exit("backtest entry not found: " + NAME)
# stream one entry at a time (raw_decode) — the only safe way to slice a
# single entry out of the half-gigabyte blob
s = data.decode(errors="replace")
del data
dec = json.JSONDecoder()
i = s.index("[") + 1
n = len(s)
obj = None
while True:
    while i < n and s[i] in " \t\r\n,":
        i += 1
    if i >= n or s[i] == "]":
        break
    o, i = dec.raw_decode(s, i)
    if o.get("name") == NAME:
        obj = o
        break
if obj is None:
    sys.exit("entry not decoded: " + NAME)
tr = obj.get("trades") or []
print("component trades:", len(tr))

df = pd.read_parquet(PARQ)
tcol = [c for c in df.columns if c.lower() in ("t", "time", "ts", "datetime",
                                               "date")][0]
df[tcol] = pd.to_datetime(df[tcol])
ser = df.set_index(tcol)["close"]
print("1m bars:", len(ser), ser.index[0], "->", ser.index[-1])

rows = []
n_long = 0
for t in tr:
    if float(t.get("dir", 1)) < 0:
        continue
    n_long += 1
    try:
        e_t = pd.to_datetime(t["entry_t"]); x_t = pd.to_datetime(t["exit_t"])
        e_px = float(t["entry_px"]); x_px = float(t["exit_px"])
    except Exception:
        continue
    w = ser[(ser.index > e_t) & (ser.index <= x_t)]
    if len(w) < 5:
        continue
    red = w[w <= e_px * (1 - RED)]
    if not len(red):
        continue
    j0 = w.index.get_loc(red.index[0])
    seg = w.iloc[j0:]
    entry = seg.iloc[0]; inpos = True; last_exit = None; net = 0.0; cyc = 1
    for p in seg.iloc[1:]:
        if inpos and p >= entry * (1 + TP):
            net += (p / entry - 1) - 2 * FEE; inpos = False; last_exit = p
        elif not inpos and p <= last_exit * (1 - DIP):
            entry = p; inpos = True; cyc += 1
    if inpos:
        net += (seg.iloc[-1] / entry - 1) - 2 * FEE
    adopt_hold = (x_px / seg.iloc[0] - 1) - 2 * FEE
    rows.append((net, adopt_hold, cyc, str(e_t)[:16]))

print()
print("windows where the virtual went >=%.0f%% red: %d of %d long trades"
      % (100 * RED, len(rows), n_long))
print("inside those windows (adopt at first red bar), tp=%.2g%% dip=%.2g%%:"
      % (100 * TP, 100 * DIP))
print("  late-join & mirror-out (current): %+.1f%% total"
      % (100 * sum(a for _, a, _, _ in rows)))
print("  harvest overlay:                  %+.1f%% total  (%d cycles)"
      % (100 * sum(n for n, _, _, _ in rows), sum(c for _, _, c, _ in rows)))
print("  overlay wins %d / %d windows"
      % (sum(1 for n, a, _, _ in rows if n > a), len(rows)))
d = sorted((n - a, w) for n, a, _, w in rows)
print("  worst deltas:", ["%+.2f%% @%s" % (100 * x, w) for x, w in d[:3]])
print("  best deltas: ", ["%+.2f%% @%s" % (100 * x, w) for x, w in d[-3:]])
