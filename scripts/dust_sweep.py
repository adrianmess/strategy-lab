#!/usr/bin/env python3
"""TRUE-ZERO dust sweep via MEXC's Dust Transfer API (the app's broom
button): converts sub-tradable balances (e.g. 0.008 SOL) into MX.
Run it YOURSELF — it converts real assets:

    ssh -i ~/.ssh/lab_auto_ed25519 -o IdentitiesOnly=yes admn@admns-Mac-mini.local \
        '~/venv/bin/python3 ~/strategy-lab/scripts/dust_sweep.py --yes [--asset SOL]'

Without --yes it only LISTS what MEXC says is convertible (safe, read-only).
MEXC allows the sweep roughly once per 24h."""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/strategy-lab/adaptive_trader"))
from mexc_api import MexcSpotAPI  # noqa: E402

acct = "mexc2"
asset = None
if "--asset" in sys.argv:
    asset = sys.argv[sys.argv.index("--asset") + 1].upper()

api = MexcSpotAPI(account=acct)
try:
    lst = api._signed("GET", "/api/v3/capital/convert/list", {})
except Exception as e:
    raise SystemExit(f"convert/list failed: {e}")
print("convertible per MEXC:", lst if not isinstance(lst, list)
      else [(x.get("asset") or x.get("currency"), x.get("balance")
             or x.get("convertMx")) for x in lst])

if "--yes" not in sys.argv:
    raise SystemExit("read-only preview done. Re-run with --yes to convert "
                     "(optionally --asset SOL to sweep just one).")

names = []
if isinstance(lst, list):
    names = [str(x.get("asset") or x.get("currency")) for x in lst
             if x.get("asset") or x.get("currency")]
if asset:
    names = [a for a in names if a == asset] or [asset]
if not names:
    raise SystemExit("nothing convertible right now")
try:
    res = api._signed("POST", "/api/v3/capital/convert",
                      {"asset": ",".join(names)})
except Exception as e:
    raise SystemExit(f"convert failed: {e}")
print("converted:", res)
print("SOL balance now:", api.balance("SOL"))
