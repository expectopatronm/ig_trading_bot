#!/usr/bin/env python3
"""
main.py — IG REST DAX scalper (DEMO), modular + quota reporting.

Adds periodic quota/call-usage reporting:
- Rolling 60s windows for trade/data/auth/other
- Rolling 7-day historical datapoint usage (candles fetched)
- Surfaces any X-RateLimit-* headers if IG sends them

Aim & goal unchanged:
- Per-trade TP ≈ €1; Daily ≈ €10
- SL distance = 3× TP (>= IG min)
- ATR/spread gates; EMA(20) invalidation; breakeven + ATR trailing
- Session filter Europe/Berlin
"""

import json
import logging
import os
import signal
import sys
import threading
import time

from ig_client import IGRest
from sizing import choose_germany40_epic, compute_size_and_distances
from sessions import is_within_sessions
from strategies import choose_direction
from indicators import compute_atr_points, latest_mid_and_spread
from risk import trade_manager
from quota import RateLimits, QuotaTracker, QuotaReporter
from config import (
    PER_TRADE_TARGET_EUR, DAILY_TARGET_EUR,
    ATR_PERIOD, ATR_MIN_THRESHOLD, SPREAD_MAX_POINTS,
    SESSION_IDLE_SLEEP_SECONDS, SCALP_STRATEGY,
    RETRY_BACKOFF_SEC,
    QUOTA_REPORT_EVERY_SEC, EST_TRADE_PER_MIN, EST_DATA_PER_MIN, EST_HIST_POINTS_WEEK
)

stop_event = threading.Event()


def close_all_positions(ig: IGRest, epic: str | None = None) -> None:
    """Attempt to close all (or only matching) open positions at market."""
    try:
        pos = ig.list_positions()
    except Exception as e:
        logging.error("%s", e)
        return
    for p in pos.get("positions", []):
        position = p.get("position", {}) or {}
        market = p.get("market", {}) or {}
        deal_id = position.get("dealId")
        direction = position.get("direction")
        try:
            size = float(position.get("size", 0) or 0)
        except Exception as e:
            logging.error("%s", e)
            continue
        inst_epic = position.get("epic") or market.get("epic")
        expiry = position.get("expiry") or market.get("expiry") or "-"
        currency = position.get("currency") or market.get("currency") or "EUR"
        if not deal_id or size <= 0:
            continue
        if epic and inst_epic != epic:
            continue
        try:
            ref = ig.close_position_market(deal_id, direction, size,
                                           epic=inst_epic, expiry=expiry, currency=currency)
            logging.info("Closed %s (%s) size=%.2f dealRef=%s", inst_epic, direction, size, ref)
        except Exception as e:
            logging.error("%s", e)


def main():
    """Run the demo scalper until target is reached or a stop signal arrives."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    api_key = os.environ.get("API_KEY")
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    account_id = os.environ.get("ACCOUNT_ID")

    if not all([api_key, username, password]):
        print("Set API_KEY, USERNAME, PASSWORD (and ACCOUNT_ID) as env vars.", file=sys.stderr)
        sys.exit(2)

    # --- Quota tracker + reporter
    limits = RateLimits(
        trade_per_min=EST_TRADE_PER_MIN,
        data_per_min=EST_DATA_PER_MIN,
        hist_points_week=EST_HIST_POINTS_WEEK
    )
    tracker = QuotaTracker(limits)
    reporter = QuotaReporter(tracker, interval_sec=QUOTA_REPORT_EVERY_SEC, stop_evt=stop_event)
    reporter.start()

    ig = IGRest(api_key, username, password, account_id, tracker=tracker)

    def handle_signal(sig, frame):
        logging.warning("Signal %s received: closing positions and shutting down…", sig)
        stop_event.set()
        try:
            close_all_positions(ig, epic=None)
        finally:
            try:
                ig.logout()
            except Exception as e:
                logging.error("%s", e)
            sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        ig.login()

        epic, details = choose_germany40_epic(ig)

        size, tp_pts, sl_pts, currency = compute_size_and_distances(details, PER_TRADE_TARGET_EUR, 500.0)
        logging.info("Initial sizing: size=%.2f, TP=%.2f pts, SL=%.2f pts, currency=%s", size, tp_pts, sl_pts, currency)

        ig_min_stop_points = float(
            details.get("dealingRules", {}).get("minNormalStopOrLimitDistance", {}).get("value", 0.1))

        realized_approx = 0.0
        trade_num = 0

        while (realized_approx + 1e-6) < DAILY_TARGET_EUR and not stop_event.is_set():
            if not is_within_sessions():
                logging.info("Outside session window. Sleeping %.0f sec…", SESSION_IDLE_SLEEP_SECONDS)
                time.sleep(SESSION_IDLE_SLEEP_SECONDS)
                continue

            # Pre-entry volatility & cost gates
            try:
                bars = ig.recent_prices(epic, "MINUTE", max(ATR_PERIOD + 2, 30)).get("prices", [])
            except Exception as e:
                logging.error("%s", e)
                time.sleep(5.0)
                continue
            if not bars:
                time.sleep(2.0)
                continue

            atr = compute_atr_points(bars, ATR_PERIOD)
            last_mid, spread = latest_mid_and_spread(bars)
            if (atr != atr) or last_mid is None or atr < ATR_MIN_THRESHOLD or (spread is not None and spread > SPREAD_MAX_POINTS):
                logging.info("Skip entry: ATR=%.2f (min %.2f), spread=%s (max %.2f)",
                             atr, ATR_MIN_THRESHOLD, f"{spread:.2f}" if spread is not None else "n/a", SPREAD_MAX_POINTS)
                time.sleep(5.0)
                continue

            # ===== Strategy-driven direction =====
            direction = choose_direction(ig, epic, SCALP_STRATEGY)
            if not direction:
                logging.info("No %s signal; waiting…", SCALP_STRATEGY)
                time.sleep(5.0)
                continue

            is_long = (direction.upper() == "BUY")
            trade_num += 1
            logging.info("Trade #%d (%s): %s %s size=%.2f | TP=%.2f pts (≈€%.2f), SL=%.2f pts",
                         trade_num, SCALP_STRATEGY, direction, epic, size, tp_pts, PER_TRADE_TARGET_EUR, sl_pts)

            # ===== Place order =====
            try:
                deal_ref, confirm = ig.open_market_position(
                    epic=epic,
                    direction=direction,
                    size=size,
                    currency=currency,
                    limit_distance_points=tp_pts,
                    stop_distance_points=sl_pts,
                )
            except Exception as e:
                logging.error("%s", e)
                time.sleep(RETRY_BACKOFF_SEC)
                continue

            status = (confirm.get("dealStatus") or confirm.get("status") or "").upper()
            deal_id = confirm.get("dealId")
            if status not in ("ACCEPTED", "OPENED", "FILLED") or not deal_id:
                logging.error("Deal not accepted: %s | confirm=%s", status, json.dumps(confirm)[:300])
                time.sleep(RETRY_BACKOFF_SEC)
                continue

            # Determine entry level
            try:
                pos = ig.list_positions()
                entry_level = None
                for p in pos.get("positions", []):
                    if p.get("position", {}).get("dealId") == deal_id:
                        entry_level = float(p["position"]["level"])
                        break
                if entry_level is None:
                    entry_level = float(last_mid) if last_mid is not None else None
            except Exception as e:
                logging.error("%s", e)
                entry_level = float(last_mid) if last_mid is not None else None

            # ===== Manage trade until exit =====
            favourable = trade_manager(
                ig=ig,
                deal_id=deal_id,
                epic=epic,
                currency=currency,
                is_long=is_long,
                entry_level=entry_level,
                tp_pts=tp_pts,
                ig_min_stop_points=ig_min_stop_points,
                stop_event=stop_event
            )

            if favourable:
                realized_approx += PER_TRADE_TARGET_EUR
                logging.info("Favourable exit. Session realized ≈ €%.2f / €%.2f target",
                             realized_approx, DAILY_TARGET_EUR)
            else:
                logging.info("Unfavourable/neutral exit not adding to €1-per-trade goal.")

            # Re-check sizing in case IG min distances changed
            try:
                details = ig.market_details(epic)
                new_size, new_tp, new_sl, _ = compute_size_and_distances(details, PER_TRADE_TARGET_EUR, 500.0)
                if (abs(new_size - size) > 1e-6) or (abs(new_tp - tp_pts) > 1e-6):
                    size, tp_pts, sl_pts = new_size, new_tp, new_sl
                    logging.info("Adjusted sizing: size=%.2f, TP=%.2f, SL=%.2f", size, tp_pts, sl_pts)
            except Exception as e:
                logging.error("%s", e)

        logging.info("Target reached or stop requested. Tidying up…")
        close_all_positions(ig, epic=None)

    except Exception as e:
        logging.error("%s", e)
    finally:
        try:
            ig.logout()
        except Exception as e:
            logging.error("%s", e)
        logging.info("Logged out. Bye.")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=True)
    except Exception as e:
        logging.error("%s", e)
    main()
