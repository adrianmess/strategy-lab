#!/usr/bin/env python3
"""Resumable CoinAPI 3MIN OHLCV downloader for MEXCFTS_PERP_SOL_USDT."""
import json, os, sys, time
import urllib.request

API_KEY = "caaddd33-6801-48b9-a48f-642ba05bffb5"
SYMBOL = "MEXCFTS_PERP_SOL_USDT"
PERIOD = "3MIN"
OUT = "/sessions/festive-happy-gauss/mnt/outputs/sol_3min.jsonl"
STATE = "/sessions/festive-happy-gauss/mnt/outputs/download_state.json"
DATA_START = "2023-11-27T00:00:00"
DATA_END = "2026-07-02T00:00:00"

state = {"time_start": DATA_START, "done": False}
if os.path.exists(STATE):
    state = json.load(open(STATE))
if state.get("done"):
    print("Already done")
    sys.exit(0)

deadline = time.time() + 38  # stay under bash timeout
n = 0
while time.time() < deadline:
    url = (f"https://rest.coinapi.io/v1/ohlcv/{SYMBOL}/history?"
           f"period_id={PERIOD}&time_start={state['time_start']}&time_end={DATA_END}&limit=100000&include_empty_items=false")
    req = urllib.request.Request(url, headers={"X-CoinAPI-Key": API_KEY, "Accept-Encoding": "gzip"})
    import gzip, io
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    rows = json.loads(raw)
    if not rows:
        state["done"] = True
        break
    with open(OUT, "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    n += len(rows)
    last = rows[-1]["time_period_end"]
    state["time_start"] = last
    if len(rows) < 100000 and last >= "2026-07-01":
        state["done"] = True
        break

json.dump(state, open(STATE, "w"))
print(f"fetched {n} rows this run; cursor={state['time_start']}; done={state.get('done')}")
