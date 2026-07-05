#!/usr/bin/env python3
"""Adaptive live trader.

Main loop:
  - maintain 3m/1m kline history from MEXC public API
  - on each new closed 3m bar: evaluate the adaptive strategy
  - execute via the existing Playwright webhook server (webhook_server.py)
  - protective stop also enforced intra-bar on every poll

Run:  python3 trader.py            (uses config.json, starts in dry_run)
      python3 trader.py --live     (POSTs to the webhook server)
"""
import argparse
import json
import logging
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from data_feed import Feed              # noqa: E402

CFG_PATH = os.path.join(HERE, "config.json")


def make_strategy(cfg, state):
    """Route by candidate format: V7 (engine3 full-param, 'regs' list),
    V6 (wf2 format), or legacy params dict."""
    cand = cfg.get("candidate")
    if cand and (cand.get("strategy") == "v7" or "regs" in cand):
        from strategy_v7 import StrategyV7
        return StrategyV7(cfg, state)
    if cand:
        from strategy_v6 import StrategyV6
        return StrategyV6(cfg, state)
    from strategy import Strategy
    return Strategy(cfg, state)


def setup_logging(cfg):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(HERE, cfg["log_file"]))],
    )


def load_state(cfg):
    path = os.path.join(HERE, cfg["state_file"])
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(cfg, state):
    path = os.path.join(HERE, cfg["state_file"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=float)
    os.replace(tmp, path)


class Executor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("executor")

    def _post(self, payload):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would POST: %s", json.dumps(payload))
            return {"status": "dry_run"}
        r = requests.post(self.cfg["webhook_url"], json=payload, timeout=120)
        r.raise_for_status()
        out = r.json()
        self.log.info("webhook response: %s", out)
        return out

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        notional = cfg["equity_usdt"] * lev
        qty = int(notional / price / cfg["contract_size"])
        if qty < 1:
            self.log.warning("qty < 1 contract, skipping (equity too small)")
            return None, 0
        action = "open_long" if direction > 0 else "open_short"
        res = self._post({"action": action, "symbol": cfg["symbol"],
                          "leverage": max(1, int(round(lev))), "quantity": qty})
        return res, qty

    def close_position(self):
        return self._post({"action": "close_position", "symbol": self.cfg["symbol"]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="disable dry_run")
    ap.add_argument("--config", default=os.environ.get("TRADER_CONFIG", "config.json"),
                    help="config file name (in this directory) or absolute path")
    args = ap.parse_args()

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(HERE, args.config)
    with open(cfg_path) as f:
        cfg = json.load(f)
    if args.live:
        cfg["dry_run"] = False
    setup_logging(cfg)
    log = logging.getLogger("trader")
    log.info("starting (dry_run=%s)", cfg["dry_run"])

    state = load_state(cfg)
    strat = make_strategy(cfg, state)
    ex = Executor(cfg)

    feed = Feed(cfg["symbol"])
    feed.backfill()
    last_closed = None

    while True:
        try:
            feed.update()
            price = feed.last_price()

            # 1) intra-bar protective check (every poll)
            check = getattr(strat, "intrabar_check", None) or strat.intrabar_stop
            act = check(price)
            if act:
                log.warning("INTRABAR STOP at %.3f: %s", price, act)
                ex.close_position()
                state["position"] = None
                save_state(cfg, state)

            # 2) new closed bar?
            closed = feed.closed_bars()
            newest = closed["t"].iloc[-1] if len(closed) else None
            if newest is not None and newest != last_closed:
                last_closed = newest
                lo = closed["t"].iloc[0]
                d1 = feed.df1[feed.df1["t"] >= lo].reset_index(drop=True)
                actions = strat.on_bar_close(closed, d1)
                for a in actions:
                    if a["do"] == "close" and state.get("position"):
                        log.info("CLOSE (%s) pos=%s", a["reason"], state["position"])
                        ex.close_position()
                        state["position"] = None
                    elif a["do"] == "open" and not state.get("position"):
                        res, qty = ex.open_position(a["dir"], a["lev"], price)
                        if qty > 0:
                            state["position"] = dict(
                                dir=a["dir"], system=a["system"], regime=a["regime"],
                                entry_price=price, qty=qty, lev=a["lev"],
                                sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"],
                                opened_at=str(newest))
                            log.info("OPEN %s %s lev=%.2f qty=%d sl=%.3f regime=%d",
                                     "LONG" if a["dir"] > 0 else "SHORT", a["system"],
                                     a["lev"], qty, a["sl_price"], a["regime"])
                save_state(cfg, state)

            time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.exception("loop error: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
