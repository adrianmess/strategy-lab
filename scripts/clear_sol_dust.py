#!/usr/bin/env python3
"""One-shot dust cleanup for mexc2 spot SOL: buy ~$1, then sell the maximum
sellable (floored) amount. Run it YOURSELF — it places real orders:

    ssh -i ~/.ssh/lab_auto_ed25519 -o IdentitiesOnly=yes admn@admns-Mac-mini.local \
        '~/venv/bin/python3 ~/strategy-lab/scripts/clear_sol_dust.py --yes'

Each pass re-rolls the sub-0.01 remainder; repeat if the leftover still
bothers you (or use MEXC's convert-small-balances-to-MX for a true zero)."""
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/strategy-lab/adaptive_trader"))
from mexc_api import MexcSpotAPI  # noqa: E402

if "--yes" not in sys.argv:
    raise SystemExit("This places REAL orders on mexc2. Re-run with --yes.")

api = MexcSpotAPI(account="mexc2")
free0 = float(api.balance("SOL") or 0)
print(f"SOL before: {free0}")
if free0 >= 0.01:
    print("already >= one sell increment — skipping the buy")
else:
    r = api.market_buy_quote("SOL_USDT", 1.0)
    print("bought:", r.get("executedQty") or r)
    time.sleep(2)
free = float(api.balance("SOL") or 0)
sell, sc = api.floor_qty("SOL_USDT", free)
if sell <= 0:
    raise SystemExit(f"nothing sellable at scale {sc} (holding {free})")
r = api.market_sell("SOL_USDT", sell)
print(f"sold {sell} (order {r.get('orderId')})")
time.sleep(2)
print(f"SOL after: {api.balance('SOL')}")
