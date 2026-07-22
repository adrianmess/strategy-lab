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
from notify import notify               # noqa: E402


def _acct_label(cfg):
    exe = cfg.get("execution") == "api" and "api" or "browser"
    acct = cfg.get("api_account") or "mexc1"
    return f"{acct}/{exe}"

CFG_PATH = os.path.join(HERE, "config.json")


def make_strategy(cfg, state):
    """Route by candidate format: V7 (engine3 full-param, 'regs' list),
    V6 (wf2 format), or legacy params dict."""
    cand = cfg.get("candidate")
    if cand and cand.get("strategy") == "metax":
        from strategy_metax import StrategyMetax
        return StrategyMetax(cfg, state)
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
                          # FLOOR, never round up: a fractional-leverage config
                          # (pre-integer-search) must not trade at MORE leverage
                          # than it was backtested with
                          "leverage": max(1, int(lev)), "quantity": qty})
        return res, qty

    def close_position(self):
        return self._post({"action": "close_position", "symbol": self.cfg["symbol"]})


class APIExecutor:
    """Native MEXC futures API execution (config: "execution": "api").
    Same interface as Executor; dry_run only logs, exactly like the webhook
    path. NOTE: futures only — MEXC spot API trading is still restricted to
    selected BTC/ETH pairs, so spot instances keep the browser executor."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("api-executor")
        if cfg.get("mode") == "spot":
            raise SystemExit('execution:"api" is futures-only — MEXC spot API '
                             "trading is restricted (BTC/ETH pairs only). "
                             "Use the browser executor for spot.")
        from mexc_api import MexcFuturesAPI
        self.api = MexcFuturesAPI(account=cfg.get("api_account"))
        self.log.info("MEXC futures API executor ready (account=%s, proxy=%s)",
                      self.api.account, bool(self.api.proxies))

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        notional = cfg["equity_usdt"] * lev
        qty = int(notional / price / cfg["contract_size"])
        if qty < 1:
            self.log.warning("qty < 1 contract, skipping (equity too small)")
            return None, 0
        lev_i = max(1, int(lev))   # FLOOR — never more leverage than backtested
        if cfg["dry_run"]:
            self.log.info("[DRY RUN] would API-%s %d contracts at ~%.3f lev %d",
                          "LONG" if direction > 0 else "SHORT", qty, price, lev_i)
            return {"status": "dry_run"}, qty
        try:
            fn = self.api.open_long if direction > 0 else self.api.open_short
            res = fn(cfg["symbol"], qty, lev_i, price)
            self.log.info("API order placed: %s", res)
            return {"status": "success", "order": res}, qty
        except Exception as e:
            self.log.error("API order FAILED: %s", e)
            return {"status": "error", "message": str(e)}, 0

    def close_position(self):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would API-close all %s positions",
                          self.cfg["symbol"])
            return {"status": "dry_run"}
        try:
            res = self.api.close_position(self.cfg["symbol"])
            self.log.info("API close: %s", res)
            return {"status": "success", "result": res}
        except Exception as e:
            self.log.error("API close FAILED: %s", e)
            return {"status": "error", "message": str(e)}


class APISpotExecutor:
    """Native MEXC SPOT API execution (config: "execution": "api" + mode spot).
    Market BUY spends equity_usdt of USDT; market SELL closes the tracked
    quantity (read from the state file, falling back to the free SOL balance,
    capped so personal holdings on the account are never touched beyond it)."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("api-spot-executor")
        from mexc_api import MexcSpotAPI
        self.api = MexcSpotAPI(account=cfg.get("api_account"))
        base = cfg["symbol"].split("_")[0]
        self.base_asset = base
        self.log.info("MEXC SPOT API executor ready (account=%s, proxy=%s)",
                      self.api.account, bool(self.api.proxies))

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        if direction < 0:
            self.log.error("spot cannot short — signal ignored")
            return {"status": "error", "message": "spot cannot short"}, 0
        quote = float(cfg["equity_usdt"])       # spot is always 1x
        qty_est = quote / price
        if cfg["dry_run"]:
            self.log.info("[DRY RUN] would SPOT-BUY %.2f USDT (~%.4f %s) at ~%.3f",
                          quote, qty_est, self.base_asset, price)
            return {"status": "dry_run"}, qty_est
        try:
            res = self.api.market_buy_quote(cfg["symbol"], quote)
            qty = float(res.get("executedQty") or 0) or qty_est
            self.log.info("SPOT BUY filled: qty=%.4f order=%s", qty,
                          res.get("orderId"))
            return {"status": "success", "order": res}, qty
        except Exception as e:
            self.log.error("SPOT BUY FAILED: %s", e)
            return {"status": "error", "message": str(e)}, 0

    def _tracked_qty(self):
        try:
            st = json.load(open(os.path.join(HERE, self.cfg["state_file"])))
            pos = st.get("position") or {}
            return float(pos.get("qty") or 0)
        except Exception:
            return 0.0

    def close_position(self):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would SPOT-SELL tracked %s position",
                          self.base_asset)
            return {"status": "dry_run"}
        try:
            qty = self._tracked_qty()
            free = self.api.balance(self.base_asset)
            sell = min(qty, free) if qty > 0 else free
            if sell <= 0:
                self.log.warning("nothing to sell (tracked=%.4f free=%.4f)",
                                 qty, free)
                return {"status": "success", "note": "nothing to sell"}
            res = self.api.market_sell(self.cfg["symbol"], sell)
            self.log.info("SPOT SELL filled: qty=%.4f order=%s", sell,
                          res.get("orderId"))
            return {"status": "success", "order": res}
        except Exception as e:
            self.log.error("SPOT SELL FAILED: %s", e)
            return {"status": "error", "message": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="disable dry_run")
    ap.add_argument("--config", default=os.environ.get("TRADER_CONFIG", "config.json"),
                    help="config file name (in this directory) or absolute path")
    args = ap.parse_args()

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(HERE, args.config)
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["_path"] = cfg_path   # router strategies hot-reload re-assignments
    if args.live:
        cfg["dry_run"] = False
    setup_logging(cfg)
    log = logging.getLogger("trader")
    log.info("starting (dry_run=%s)", cfg["dry_run"])

    state = load_state(cfg)
    strat = make_strategy(cfg, state)
    if cfg.get("execution") == "api":
        ex = APISpotExecutor(cfg) if cfg.get("mode") == "spot" else APIExecutor(cfg)
        log.info("execution path: MEXC %s API",
                 "SPOT" if cfg.get("mode") == "spot" else "futures")
    else:
        ex = Executor(cfg)
        log.info("execution path: browser webhook")

    is_router = (cfg.get("candidate") or {}).get("strategy") == "metax"
    feed = Feed(cfg["symbol"], anchored=is_router)
    feed.backfill()
    last_closed = None

    check = getattr(strat, "intrabar_check", None) or strat.intrabar_stop

    def protective_check():
        """Run the intra-bar protective/emergency stop against the LIVE price.
        Returns True if it closed the position. Single-threaded (called only
        from the main loop) so it can never race the bar-close order logic."""
        if not state.get("position"):
            return False
        price = feed.last_price()
        act = check(price)
        if act:
            log.warning("INTRABAR STOP at %.3f (%s price): %s",
                        price, feed.price_source(), act)
            res = ex.close_position()
            notify("position_closed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"], reason=act.get("reason"),
                   price=price, live=(not cfg["dry_run"]),
                   result=(res or {}).get("status"))
            state["position"] = None
            save_state(cfg, state)
            return True
        return False

    # How often to re-check the protective stop against live ticks, in seconds.
    # Defaults to 0.5s (near-live); heavy work (kline fetch + bar-close eval)
    # still runs once per poll_seconds. Set protect_poll_seconds <= 0 to restore
    # the old single-check-per-poll behavior.
    protect_dt = cfg.get("protect_poll_seconds", 0.5)

    while True:
        try:
            # anchored feed may only re-anchor its window while we're flat
            feed.trim_ok = (not state.get("position")
                            and not state.get("mirror"))
            feed.update()
            price = feed.last_price()

            # 1) intra-bar protective check (live price)
            protective_check()

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
                        res = ex.close_position()
                        notify("position_closed", account=_acct_label(cfg),
                               config=os.path.basename(cfg.get("_path", "?")),
                               symbol=cfg["symbol"], reason=a["reason"],
                               price=price, live=(not cfg["dry_run"]),
                               position=state.get("position"),
                               result=(res or {}).get("status"))
                        if (res or {}).get("status") == "error":
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="close", detail=res.get("message"))
                        state["position"] = None
                    elif a["do"] == "open" and not state.get("position"):
                        res, qty = ex.open_position(a["dir"], a["lev"], price)
                        if (res or {}).get("status") == "error":
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="open", detail=res.get("message"))
                        if qty > 0:
                            notify("position_opened", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   symbol=cfg["symbol"],
                                   side=("LONG" if a["dir"] > 0 else "SHORT"),
                                   qty=qty, lev=a["lev"], price=price,
                                   live=(not cfg["dry_run"]))
                            state["position"] = dict(
                                dir=a["dir"], system=a["system"], regime=a["regime"],
                                entry_price=price, qty=qty, lev=a["lev"],
                                sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"],
                                opened_at=str(newest))
                            log.info("OPEN %s %s lev=%.2f qty=%d sl=%.3f regime=%d",
                                     "LONG" if a["dir"] > 0 else "SHORT", a["system"],
                                     a["lev"], qty, a["sl_price"], a["regime"])
                save_state(cfg, state)

            # 3) fast protective sub-loop: keep watching the LIVE price between
            # heavy polls so an adverse move is caught within ~protect_dt, not
            # after the full poll interval.
            if protect_dt and protect_dt > 0:
                t_end = time.time() + cfg["poll_seconds"]
                while time.time() < t_end:
                    time.sleep(min(protect_dt, max(0.0, t_end - time.time())))
                    if protective_check():
                        break
            else:
                time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.exception("loop error: %s", e)
            notify("trader_error", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=str(e)[:300])
            time.sleep(30)


if __name__ == "__main__":
    main()
