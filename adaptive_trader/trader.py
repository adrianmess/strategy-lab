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


def _close_ok(res):
    """A close only counts if the venue CONFIRMED it. Clearing state on a
    failed close (proxy timeout, API rejection) strands a real position that
    no config tracks any more — and frees the slot to open a second one on
    top of it. Anything that is not an explicit success keeps the position."""
    return (res or {}).get("status") in ("success", "dry_run")


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
        # match APIExecutor's contract: never raise into the trading loop —
        # a raise used to skip the order_failed notify AND the state save
        try:
            r = requests.post(self.cfg["webhook_url"], json=payload,
                              timeout=120)
            r.raise_for_status()
            out = r.json()
        except Exception as e:
            self.log.error("webhook FAILED: %s", e)
            return {"status": "error", "message": str(e)}
        self.log.info("webhook response: %s", out)
        if str(out.get("status", "")).lower() in ("error", "failed"):
            return {"status": "error", "message": str(out)[:300]}
        return out if out.get("status") else {"status": "success", "raw": out}

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
        # LIVE sizing = 100% of what the account actually has (the backtests
        # compound full equity every trade — a fixed equity_usdt either
        # bounces orders when the account is smaller, e.g. code 2005
        # "Balance insufficient", or undertrades when it has grown).
        # equity_usdt remains the DRY-RUN paper size and the fallback.
        margin = float(cfg["equity_usdt"])
        if not cfg["dry_run"]:
            try:
                u = [a for a in self.api.assets()
                     if a.get("currency") == "USDT"]
                avail = float((u[0].get("availableOpen")
                               or u[0].get("availableBalance") or 0)) if u else 0.0
                if avail > 0:
                    margin = avail * 0.98   # fee/rounding headroom
                    self.log.info("sizing from live balance: %.2f USDT "
                                  "available -> margin %.2f", avail, margin)
            except Exception as e:
                self.log.warning("balance query failed (%s) — falling back "
                                 "to equity_usdt=%.2f", e, margin)
        notional = margin * lev
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
            held, fill = self._confirm_fill(direction)
            if held > 0:
                return {"status": "success", "order": res,
                        "fill_price": fill}, held
            return {"status": "success", "order": res}, qty
        except Exception as e:
            # A TIMEOUT IS NOT A REJECTION: the exchange may have accepted the
            # order and the reply died on the proxy. Ask what we actually hold
            # before declaring failure, or the next signal opens a SECOND
            # position on top of an untracked one.
            self.log.error("API order FAILED: %s", e)
            try:
                held, fill = self._confirm_fill(direction)
            except Exception as e2:
                self.log.error("post-failure position check FAILED: %s — "
                               "VERIFY THE ACCOUNT MANUALLY", e2)
                return {"status": "error", "message": str(e)}, 0
            if held > 0:
                self.log.warning("order actually FILLED despite the error "
                                 "(%s contracts @ %.6g) — adopting it",
                                 held, fill or 0)
                return {"status": "success", "adopted_after_error": True,
                        "fill_price": fill}, held
            return {"status": "error", "message": str(e)}, 0

    def _confirm_fill(self, direction, tries=3):
        """What the exchange says we hold on this symbol, after an order.
        Returns (holdVol, avg entry price) — (0, None) when flat."""
        want = 1 if direction > 0 else 2          # positionType: 1 long 2 short
        for i in range(tries):
            time.sleep(0.6 * (i + 1))             # fills settle asynchronously
            try:
                for p in (self.api.open_positions(self.cfg["symbol"]) or []):
                    if int(p.get("positionType") or 0) != want:
                        continue
                    hold = float(p.get("holdVol") or 0)
                    if hold > 0:
                        return hold, float(p.get("holdAvgPrice") or 0) or None
            except Exception:
                if i == tries - 1:
                    raise
        return 0.0, None

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
        # LIVE sizing = 100% of free USDT on the account (see APIExecutor);
        # equity_usdt is the dry-run paper size and the fallback
        quote = float(cfg["equity_usdt"])       # spot is always 1x
        if not cfg["dry_run"]:
            try:
                free = float(self.api.balance("USDT") or 0)
                if free > 0:
                    quote = free * 0.999        # rounding headroom
                    self.log.info("sizing from live balance: %.2f USDT free "
                                  "-> spending %.2f", free, quote)
            except Exception as e:
                self.log.warning("balance query failed (%s) — falling back "
                                 "to equity_usdt=%.2f", e, quote)
            # PRE-FLIGHT: MEXC rejects spot orders under 1 USDT (code 30002).
            # Firing one anyway on every signal spams the error banner and
            # the Hermes alerts, so skip quietly (one warning per dry spell).
            min_notional = float(cfg.get("min_notional_usdt", 1.0))
            if quote < min_notional:
                if not getattr(self, "_warned_low", False):
                    self._warned_low = True
                    self.log.warning(
                        "SKIPPING entries: only %.2f USDT free (minimum "
                        "order is %.2f). The account's value is probably "
                        "sitting in %s from a previous position — sell it "
                        "or fund the account to resume trading.",
                        quote, min_notional, self.base_asset)
                return {"status": "skipped", "message": "below min notional"}, 0
            self._warned_low = False
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

    def _tracked_tp(self):
        try:
            st = json.load(open(os.path.join(HERE, self.cfg["state_file"])))
            return (st.get("position") or {}).get("tp_order_id")
        except Exception:
            return None

    # ---- exchange-side take-profit net (resting GTC limit sell) ----
    def place_tp(self, qty, price):
        try:
            res = self.api.place_limit_sell(self.cfg["symbol"], qty, price)
            self.log.info("TP NET placed: sell %.6g @ %.6g (order %s)",
                          qty, price, res.get("orderId"))
            return res.get("orderId")
        except Exception as e:
            self.log.error("TP NET place FAILED: %s", e)
            return None

    def cancel_tp(self, order_id):
        try:
            self.api.cancel_order(self.cfg["symbol"], order_id)
            self.log.info("TP NET cancelled (order %s)", order_id)
            return True
        except Exception as e:
            self.log.warning("TP NET cancel failed (order %s): %s — may "
                             "already be filled/gone", order_id, e)
            return False

    def tp_status(self, order_id):
        try:
            return self.api.query_order(self.cfg["symbol"], order_id)
        except Exception as e:
            self.log.warning("TP NET query failed: %s", e)
            return None

    def close_position(self):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would SPOT-SELL tracked %s position",
                          self.base_asset)
            return {"status": "dry_run"}
        # cancel the resting TP net FIRST so it can't double-fire; if it
        # already filled, the free balance is 0 and the sell below no-ops
        tp = self._tracked_tp()
        if tp:
            self.cancel_tp(tp)
        try:
            qty = self._tracked_qty()
            free = self.api.balance(self.base_asset)
            if qty <= 0:
                # NEVER fall back to "sell everything": the free balance can
                # include holdings this bot never bought (and _tracked_qty
                # returns 0 on ANY read error, e.g. a state file mid-replace)
                self.log.error("close refused: no tracked quantity "
                               "(free=%.6g %s). Sell manually if these coins "
                               "are really the bot's.", free, self.base_asset)
                return {"status": "error",
                        "message": "no tracked quantity to close"}
            sell = min(qty, free)
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


_GUARD_FH = None      # module-level: the flock lives as long as the process


def _single_instance_guard(cfg):
    """Refuse to start if another trader already owns this config's STATE
    FILE. Two traders sharing state corrupt each other's position tracking
    (and on a live config would double orders). The panel checks this too,
    but it forgets running processes across its own restarts — this lock is
    held by the kernel, so it is authoritative. (Observed 2026-08-12: a
    panel restart orphaned a trader; a second one then ran the same dry-run
    config for 4 hours, double-polling MEXC and clobbering the state file.)"""
    global _GUARD_FH
    import fcntl
    sf = os.path.basename(cfg.get("state_file", "trader_state.json"))
    path = os.path.join(HERE, f".{sf}.lock")
    # r+ (not w) — 'w' truncates before we know whether we own the lock,
    # which would erase the holder's pid from the message
    fh = open(path, "r+" if os.path.exists(path) else "w+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            other = open(path).read().strip() or "unknown"
        except Exception:
            other = "unknown"
        raise SystemExit(
            f"REFUSING TO START: another trader (pid {other}) is already "
            f"running with state file '{sf}'. Stop it first "
            f"(panel Instances, or `kill {other}`).")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _GUARD_FH = fh        # keep the handle alive → keep the lock


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
    _single_instance_guard(cfg)
    # FCFS combos (multi-pair, multi-timeframe) run their own loop — delegate
    # so the panel's instance machinery / --live / dry-run all work unchanged
    if (cfg.get("candidate") or {}).get("strategy") == "fcfsx":
        from fcfs_runner import main_fcfs
        main_fcfs(cfg, args.live)
        return
    if args.live:
        cfg["dry_run"] = False
    # chart timeframe: MUST be in the environment BEFORE any research module
    # is imported (make_strategy) — regimes.DAY and engine bar/hour
    # conversions read LAB_TF at import/call time
    tf_min = int(str(cfg.get("timeframe") or "3m").rstrip("m"))
    if tf_min not in (1, 3, 5):
        raise SystemExit(f"unsupported timeframe {cfg.get('timeframe')!r} "
                         f"(supported: 1m, 3m, 5m)")
    os.environ["LAB_TF"] = str(tf_min)
    setup_logging(cfg)
    log = logging.getLogger("trader")
    log.info("starting (dry_run=%s, timeframe=%dm)", cfg["dry_run"], tf_min)

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
    feed = Feed(cfg["symbol"], anchored=is_router, tf_min=tf_min)
    feed.backfill()
    last_closed = None

    check = getattr(strat, "intrabar_check", None) or strat.intrabar_stop

    # ---------------- late-join (single-strategy families) ----------------
    # If the SIM would already be in a position (it opened while this trader
    # was down or being restarted), join it late — but ONLY in the red: our
    # entry at/better-than the sim's can only outperform the sim, while a
    # GREEN lev entry anchors liquidation at OUR worse price and can die on
    # a drawdown the sim survives. Each virtual trade is judged ONCE; green
    # skips persist in the state file, the virtual close is ignored, and
    # fresh signals resume afterward. Joined positions mirror the sim's exit
    # (engine replay), with the emergency intrabar net still active.
    # The metax router does this itself (strategy_metax); fcfsx likewise.
    _cand = cfg.get("candidate") or {}
    _fam = _cand.get("strategy") or ("v7" if "regs" in _cand else None)
    lj = {"on": _fam not in (None, "metax", "metax2", "pairx", "fcfsx")}

    def lj_replay(closed, d1):
        import strategy_metax as mx
        comp = dict(i=0, strategy=_fam, method=cfg.get("method", "vol3"),
                    cand=_cand)
        feats = mx.compute_features(closed)
        tr, op = mx.run_component_engine(comp, cfg["mode"], closed, d1, feats)
        lbl = mx._ts16(op["entry_t"]) if op is not None else None
        return op, lbl

    def lj_on_bar(closed, d1, price):
        """Startup / skip-mode / mirror-mode replay step. Returns True when
        the strategy's fresh OPEN actions must be suppressed this bar (a
        green-skipped virtual position is still open in the sim)."""
        if not lj["on"]:
            return False
        try:
            op, lbl = lj_replay(closed, d1)
        except Exception as e:
            log.info("late-join disabled (replay unavailable for '%s': %s)",
                     _fam, e)
            lj["on"] = False
            return False
        pos = state.get("position")
        # A) holding a late-joined position: the replay is the exit authority
        if pos and pos.get("late_mirror"):
            if lbl != pos["late_mirror"]:
                log.info("LATE-JOIN CLOSE: virtual trade %s ended",
                         pos["late_mirror"])
                res = ex.close_position()
                notify("position_closed", account=_acct_label(cfg),
                       config=os.path.basename(cfg.get("_path", "?")),
                       symbol=cfg["symbol"], reason="virtual_exit(late-join)",
                       price=price, live=(not cfg["dry_run"]),
                       position=pos, result=(res or {}).get("status"))
                if not _close_ok(res):
                    log.error("LATE-JOIN CLOSE FAILED (%s) — keeping the "
                              "position; will retry next bar",
                              (res or {}).get("message"))
                    return False
                state["position"] = None
                save_state(cfg, state)
            return False
        if pos:
            return False
        # B) flat
        if op is None:
            if state.get("late_skip"):
                state["late_skip"] = None
                save_state(cfg, state)
                log.info("late-join: skipped virtual trade closed — fresh "
                         "signals resume")
            return False
        ei = int(op.get("entry_idx", -1))
        if not (0 <= ei < len(closed)) or ei == len(closed) - 1:
            return False              # fresh open — normal path handles it
        if state.get("late_skip") == lbl:
            return True               # judged green earlier — keep waiting
        ep = float(closed["close"].iloc[ei])
        d = 1 if float(op.get("dir", 1)) >= 0 else -1
        red = (price < ep) if d > 0 else (price > ep)
        # DO NOT JOIN A DYING TRADE. The red-only rule protects our entry
        # price, but "deeply red" also means the SIM is close to its own
        # liquidation — and when the sim liquidates we mirror the exit and
        # eat the loss. (2026-08-19: joined an ETH short whose sim had used
        # 93% of its liquidation distance; the sim liquidated 11 minutes
        # later and the mirrored exit cost ~$426.)
        adverse = (price / ep - 1.0) * d * -1.0        # >0 = sim underwater
        lev_v = float(op.get("lev", 1.0)) if cfg["mode"] == "lev" else 1.0
        cap = float(cfg.get("late_join_max_drawdown", 0.5))
        if lev_v > 1:
            liq_dist = 1.0 / lev_v - 0.008             # fraction of price
            used = adverse / max(liq_dist, 1e-9)
            if used > cap:
                state["late_skip"] = lbl
                save_state(cfg, state)
                log.warning("LATE-JOIN REFUSED: the sim position %s is %.0f%% "
                            "of the way to ITS liquidation (%.1f%% underwater "
                            "at %gx) — joining would inherit a forced exit",
                            lbl, 100 * used, 100 * adverse, lev_v)
                return True
        elif adverse > cap * 0.2:        # spot: no liquidation, cap the bleed
            state["late_skip"] = lbl
            save_state(cfg, state)
            log.warning("LATE-JOIN REFUSED: the sim position %s is %.1f%% "
                        "underwater — beyond the join limit", lbl,
                        100 * adverse)
            return True
        # red-only is the LEVERAGED exception (a green lev entry anchors
        # liquidation at our worse price); spot cannot liquidate, so a spot
        # instance joins its virtual position regardless of color
        if cfg["mode"] != "spot" and not red:
            state["late_skip"] = lbl
            save_state(cfg, state)
            log.info("LATE-JOIN skipped: virtual pos %s @%.6g is GREEN at "
                     "%.6g (lev) — waiting for the next fresh signal",
                     lbl, ep, price)
            return True
        lev = float(op.get("lev", 1.0)) if cfg["mode"] == "lev" else 1.0
        res, qty = ex.open_position(d, lev, price)
        if (res or {}).get("status") == "error":
            notify("order_failed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   action="open", detail=res.get("message"))
            return False
        if qty and qty > 0:
            state["late_skip"] = None
            state["position"] = dict(
                dir=d, system=0, regime=0, entry_price=ep, qty=qty, lev=lev,
                sl_price=0.0,
                entry_sig_ms=float(closed["t"].iloc[ei].value // 10**6),
                opened_at=str(closed["t"].iloc[-1]), fill_price=price,
                opened_ms=int(time.time() * 1000), late_mirror=lbl)
            log.info("LATE-JOIN OPEN dir=%+d lev=%.1f qty=%s: virtual entry "
                     "%s @%.6g, filled %.6g (%s)", d, lev, qty, lbl, ep, price,
                     "red" if red else "green/spot")
            notify("position_opened", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"],
                   side=("LONG" if d > 0 else "SHORT"), qty=qty, lev=lev,
                   price=price, live=(not cfg["dry_run"]),
                   note=f"late-join in the red (virtual entry {ep:g})")
            save_state(cfg, state)
        return False

    _tp_poll = {"next": time.time() + 30}   # spot TP-net fill polling

    # manual close-override sidecar (written by the panel, polled by mtime —
    # applies to the CURRENT position, no trader restart needed once running)
    _ov_path = os.path.join(HERE, ".override_" + os.path.basename(
        cfg.get("state_file", "trader_state.json")))
    _ov = {"m": 0.0, "d": None}

    def _override_check(price):
        pos = state.get("position")
        if not pos or not price:
            return False
        try:
            m = os.path.getmtime(_ov_path)
        except OSError:
            _ov["d"] = None
            return False
        if m != _ov["m"]:
            _ov["m"] = m
            try:
                _ov["d"] = json.load(open(_ov_path))
            except Exception:
                _ov["d"] = None
        d = _ov["d"]
        if not d or str(pos.get("opened_at")) != d.get("pos_key"):
            return False
        if not d.get("now") and not ((price >= d["price"]) if d.get("above")
                                     else (price <= d["price"])):
            return False
        log.warning("MANUAL %s close at %.6g",
                    "CLOSE-NOW" if d.get("now") else
                    f"OVERRIDE (trigger {d['price']:.6g})", price)
        # CONSUME the trigger before acting: a raising executor (the browser
        # path re-raises after a 120s timeout) used to re-enter this branch
        # with the sidecar still armed and fire the close a second time.
        try:
            os.remove(_ov_path)
        except OSError:
            pass
        _ov["d"] = None
        try:
            res = ex.close_position()
        except Exception as e:
            log.error("manual close raised: %s — position kept", e)
            return False
        notify("position_closed", account=_acct_label(cfg),
               config=os.path.basename(cfg.get("_path", "?")),
               symbol=cfg["symbol"], reason="manual_override", price=price,
               live=(not cfg["dry_run"]), position=pos,
               result=(res or {}).get("status"))
        if not _close_ok(res):
            log.error("manual close FAILED (%s) — position kept",
                      (res or {}).get("message"))
            return False
        # make the manual close STICK: if this was a mirrored (late-joined)
        # position, remember the virtual trade so late-join cannot re-enter
        if pos.get("late_mirror"):
            state["late_skip"] = pos["late_mirror"]
        state["position"] = None
        save_state(cfg, state)
        return True

    def protective_check():
        """Run the intra-bar protective/emergency stop against the LIVE price.
        Returns True if it closed the position. Single-threaded (called only
        from the main loop) so it can never race the bar-close order logic."""
        if not state.get("position"):
            return False
        price = feed.last_price()
        if _override_check(price):
            return True
        if _tp_poll["next"] and time.time() >= _tp_poll["next"]:
            _tp_poll["next"] = time.time() + 30      # cheap: 1 query / 30s
            if tp_check_filled():
                return True
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
            if not _close_ok(res):
                log.error("PROTECTIVE CLOSE FAILED (%s) — position kept, "
                          "retrying next tick", (res or {}).get("message"))
                return False
            state["position"] = None
            save_state(cfg, state)
            return True
        return False

    # ---------------- exchange-side TP net (SPOT ONLY) ----------------
    # A resting GTC limit sell held ON THE EXCHANGE at the strategy's current
    # scheduled profit target — if this server dies mid-position, the take-
    # profit still executes. The trader re-syncs the order whenever the
    # pt/apt1/apt2 schedule shifts the target. Lev is deliberately excluded
    # (Adrian's call: futures protection stays software-side for now).
    tp_on = (cfg.get("mode") == "spot" and cfg.get("execution") == "api"
             and not cfg["dry_run"] and cfg.get("protective_orders", True)
             and hasattr(strat, "target_price"))
    if cfg.get("mode") == "spot" and not cfg["dry_run"]:
        log.info("exchange-side TP net: %s",
                 "ACTIVE" if tp_on else "off (needs api execution + "
                 "protective_orders + a schedule-aware strategy)")

    def tp_check_filled():
        """Did the resting TP sell execute since the last look? It used to be
        reconciled only at startup, so a mid-session fill left the trader
        holding a position that no longer existed: no new entries (idle
        capital) and a protective stop watching thin air."""
        if not tp_on:
            return False
        pos = state.get("position")
        oid = (pos or {}).get("tp_order_id")
        if not oid:
            return False
        o = ex.tp_status(oid)
        if not o:
            return False                      # query failed — decide nothing
        st = str(o.get("status") or "")
        if st == "FILLED":
            px = float(o.get("price") or 0)
            log.info("TP NET FILLED: sold @ %.6g — the exchange closed the "
                     "position for us", px)
            notify("position_closed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"], reason="tp_net_filled",
                   price=px, live=True, position=pos, result="success")
            state["position"] = None
            save_state(cfg, state)
            return True
        if st == "PARTIALLY_FILLED":
            done = float(o.get("executedQty") or 0)
            if done > 0 and pos.get("qty"):
                left = max(0.0, float(pos["qty"]) - done)
                log.warning("TP net PARTIALLY filled (%.6g of %.6g) — "
                            "tracking the remainder", done, float(pos["qty"]))
                pos["qty"] = left
                save_state(cfg, state)
        return False

    def tp_sync(now_ms):
        """Keep the resting TP sell at the CURRENT scheduled target. Late-
        joined (replay-mirrored) positions are skipped — their stored
        regime/system fields are approximations, so a computed target could
        be wrong; the replay remains their exit authority."""
        if not tp_on:
            return
        if tp_check_filled():
            return
        pos = state.get("position")
        if not pos or pos.get("late_mirror"):
            return
        try:
            tgt = float(strat.target_price(pos, now_ms))
        except Exception as e:
            log.warning("tp_sync: target computation failed: %s", e)
            return
        cur = pos.get("tp_price")
        if (cur is not None and pos.get("tp_order_id")
                and abs(tgt - cur) / max(abs(tgt), 1e-9) < 5e-4):
            return                          # unchanged (within 0.05%)
        if pos.get("tp_order_id"):
            ex.cancel_tp(pos["tp_order_id"])
        # clamp to what we actually hold: state qty is gross of the base-fee
        # deduction, and an oversized limit sell is rejected outright
        try:
            _free = float(ex.api.balance(ex.base_asset) or 0)
        except Exception:
            _free = 0.0
        _q = min(float(pos["qty"]), _free) if _free > 0 else float(pos["qty"])
        oid = ex.place_tp(_q, tgt)
        pos["tp_order_id"] = oid
        pos["tp_price"] = tgt if oid else None
        save_state(cfg, state)

    # startup reconciliation: did the TP net FILL while we were down?
    if tp_on and (state.get("position") or {}).get("tp_order_id"):
        _pos = state["position"]
        _o = ex.tp_status(_pos["tp_order_id"]) or {}
        _st = str(_o.get("status") or "")
        if _st == "FILLED":
            _px = float(_o.get("price") or 0)
            log.info("TP NET FILLED while down: sold @ %.6g — position was "
                     "closed offline by the exchange", _px)
            notify("position_closed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"], reason="tp_net_filled_offline",
                   price=_px, live=True, position=_pos, result="success")
            state["position"] = None
            save_state(cfg, state)
        elif _st in ("CANCELED", "EXPIRED"):
            _pos.pop("tp_order_id", None)
            _pos.pop("tp_price", None)
            save_state(cfg, state)
        elif not _st:
            # query FAILED (network/proxy) — do NOT assume the order is gone,
            # or tp_sync would place a second resting sell for the same coins
            log.warning("TP net status unknown for order %s — leaving it "
                        "tracked; will re-check on the next sync",
                        _pos.get("tp_order_id"))

    # How often to re-check the protective stop against live ticks, in seconds.
    # Defaults to 0.5s (near-live); heavy work (kline fetch + bar-close eval)
    # still runs once per poll_seconds. Set protect_poll_seconds <= 0 to restore
    # the old single-check-per-poll behavior.
    protect_dt = cfg.get("protect_poll_seconds", 0.5)

    net_err = {"kind": None, "n": 0, "t0": 0.0}
    while True:
        try:
            # anchored feed may only re-anchor its window while we're flat
            feed.trim_ok = (not state.get("position")
                            and not state.get("mirror"))
            feed.update()
            if net_err["kind"]:
                log.info("RECOVERED from %s after %d failed polls (%.0fs)",
                         net_err["kind"], net_err["n"],
                         time.time() - net_err["t0"])
                net_err.update(kind=None, n=0)
            price = feed.last_price()

            # 1) intra-bar protective check (live price)
            protective_check()

            # 2) new closed bar?
            closed = feed.closed_bars()
            newest = closed["t"].iloc[-1] if len(closed) else None
            if newest is not None and newest != last_closed:
                first_eval = last_closed is None
                last_closed = newest
                lo = closed["t"].iloc[0]
                d1 = feed.df1[feed.df1["t"] >= lo].reset_index(drop=True)
                # late-join replay runs only when it can matter: the first
                # bar after start, while a green skip is pending, or while
                # holding a late-joined (replay-mirrored) position
                lj_suppress = False
                if lj["on"] and (first_eval or state.get("late_skip")
                                 or (state.get("position") or {})
                                 .get("late_mirror")):
                    lj_suppress = lj_on_bar(closed, d1, price)
                actions = strat.on_bar_close(closed, d1)
                if (state.get("position") or {}).get("late_mirror"):
                    # replay is the exit authority for a late-joined position
                    actions = [a for a in actions if a["do"] != "close"]
                if lj_suppress:
                    actions = [a for a in actions if a["do"] != "open"]
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
                        if not _close_ok(res):
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="close",
                                   detail=(res or {}).get("message"))
                            log.error("CLOSE FAILED — position kept, will "
                                      "retry on the next bar")
                            continue
                        state["position"] = None
                        save_state(cfg, state)
                    elif a["do"] == "open" and not state.get("position"):
                        res, qty = ex.open_position(a["dir"], a["lev"], price)
                        if (res or {}).get("status") == "error":
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="open", detail=res.get("message"))
                            continue          # never record a phantom entry
                        if qty > 0:
                            notify("position_opened", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   symbol=cfg["symbol"],
                                   side=("LONG" if a["dir"] > 0 else "SHORT"),
                                   qty=qty, lev=a["lev"], price=price,
                                   live=(not cfg["dry_run"]))
                            state["position"] = dict(
                                dir=a["dir"], system=a["system"], regime=a["regime"],
                                # the venue's own fill price when it gave us
                                # one — the signal price is only a fallback
                                entry_price=((res or {}).get("fill_price")
                                             or price),
                                qty=qty, lev=a["lev"],
                                sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"],
                                opened_at=str(newest),
                                # UNAMBIGUOUS wall-clock of the fill: opened_at
                                # is a BAR timestamp (UTC) and was being read as
                                # local time, showing "open 0m" forever
                                opened_ms=int(time.time() * 1000))
                            log.info("OPEN %s %s lev=%.2f qty=%d sl=%.3f regime=%d",
                                     "LONG" if a["dir"] > 0 else "SHORT", a["system"],
                                     a["lev"], qty, a["sl_price"], a["regime"])
                save_state(cfg, state)
                tp_sync(float(newest.value // 10**6))

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
            save_state(cfg, state)      # never exit with unsaved position data
            break
        except requests.exceptions.RequestException as e:
            # transient network trouble (connection reset, timeout, DNS) on
            # the kline path: single-line WARNs with repeat-collapse instead
            # of a 50-line traceback per blip, NO notification unless it
            # persists (20 polls) — and the protective check keeps running
            # on the last known price instead of sleeping blind for 30s.
            kind = type(e).__name__
            if kind == net_err["kind"]:
                net_err["n"] += 1
                if net_err["n"] in (5, 20) or net_err["n"] % 60 == 0:
                    log.warning("network still failing: %s (%d polls, %.0fs)",
                                kind, net_err["n"],
                                time.time() - net_err["t0"])
                if net_err["n"] == 20:
                    notify("trader_error", account=_acct_label(cfg),
                           config=os.path.basename(cfg.get("_path", "?")),
                           detail=f"network failing for 20 polls: {e}"[:300])
            else:
                net_err.update(kind=kind, n=1, t0=time.time())
                log.warning("network error (%s): %s — retrying",
                            kind, str(e)[:160])
            try:
                protective_check()
            except Exception:
                pass
            time.sleep(5)
        except Exception as e:
            log.exception("loop error: %s", e)
            notify("trader_error", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=str(e)[:300])
            time.sleep(30)


if __name__ == "__main__":
    main()
