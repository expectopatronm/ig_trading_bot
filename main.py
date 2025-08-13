#!/usr/bin/env python3
"""
main.py — IG REST DAX scalper (DEMO), modular + quota reporting.

Changes in this update:
- Added **DAILY_MAX_LOSS_EUR** stop and **MAX_CONSECUTIVE_LOSSES** guard.
- Loop now stops if day net ≤ -DAILY_MAX_LOSS_EUR or if consecutive losses threshold is hit.
- No behaviour removed; integrates with the existing ledger & budget-aware sizing.
"""
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

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
    QUOTA_REPORT_EVERY_SEC, EST_TRADE_PER_MIN, EST_DATA_PER_MIN, EST_HIST_POINTS_WEEK,
    START_BALANCE_EUR, EFFECTIVE_LEVERAGE, MARGIN_UTILIZATION,
    DAILY_MAX_LOSS_EUR, MAX_CONSECUTIVE_LOSSES
)
from ledger import Ledger

stop_event = threading.Event()


def close_all_positions(ig: IGRest, epic: str | None = None) -> None:
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
        except Exception:
            continue
        inst_epic = position.get("epic") or market.get("epic")
        expiry = position.get("expiry") or market.get("expiry") or "-"
        currency = position.get("currency") or market.get("currency") or "EUR"
        if not deal_id or size <= 0:
            continue
        if epic and inst_epic != epic:
            continue
        try:
            ig.close_position_market(deal_id, direction, size,
                                     epic=inst_epic, expiry=expiry, currency=currency)
            logging.info("Closed %s (%s) size=%.2f", inst_epic, direction, size)
        except Exception as e:
            logging.error("%s", e)


def main():
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
            except Exception:
                pass
            sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        ig.login()

        # --- Ledger & working capital
        ledger = Ledger(start_balance_default=START_BALANCE_EUR)
        working_capital = float(ledger.day_start_balance)
        logging.info("Ledger: opening balance=€%.2f | today's start=€%.2f | targets: +€%.2f / -€%.2f",
                     ledger.balance, ledger.day_start_balance, DAILY_TARGET_EUR, DAILY_MAX_LOSS_EUR)

        # --- Market selection
        epic, details = choose_germany40_epic(ig)

        instr = details.get("instrument", {})
        points_per_pip = float(str(instr.get("onePipMeans", "1")).split()[0] or 1)
        pip_value_eur = float(instr.get("valueOfOnePip") or 1.0)

        # --- Budget-aware sizing
        size, tp_pts, sl_pts, currency, ok = compute_size_and_distances(
            details=details,
            target_eur=PER_TRADE_TARGET_EUR,
            working_capital_eur=working_capital,
            effective_leverage=EFFECTIVE_LEVERAGE,
            margin_utilization=MARGIN_UTILIZATION,
        )
        if not ok or size <= 0:
            logging.info("Min size exceeds budget (W=€%.2f, L=%.1fx). Waiting…", working_capital, EFFECTIVE_LEVERAGE)
            while not stop_event.is_set():
                time.sleep(30.0)
            return

        ig_min_stop_points = float(details.get("dealingRules", {}).get("minNormalStopOrLimitDistance", {}).get("value", 0.1))
        trade_num = 0
        consecutive_losses = 0

        while not stop_event.is_set():
            # Day-level guards BEFORE attempting a new trade
            if ledger.day_net() >= DAILY_TARGET_EUR - 1e-6:
                logging.info("Daily profit target reached: +€%.2f >= +€%.2f", ledger.day_net(), DAILY_TARGET_EUR)
                break
            if ledger.day_net() <= -DAILY_MAX_LOSS_EUR + 1e-6:
                logging.info("Daily loss limit hit: €%.2f ≤ -€%.2f — stopping for the day.", ledger.day_net(), DAILY_MAX_LOSS_EUR)
                break
            if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logging.info("Max consecutive losses reached (%d) — stopping for the day.", MAX_CONSECUTIVE_LOSSES)
                break

            if not is_within_sessions():
                logging.info("Outside session window. Sleeping %.0f sec…", SESSION_IDLE_SLEEP_SECONDS)
                time.sleep(SESSION_IDLE_SLEEP_SECONDS)
                continue

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

            direction = choose_direction(ig, epic, SCALP_STRATEGY)
            if not direction:
                logging.info("No %s signal; waiting…", SCALP_STRATEGY)
                time.sleep(5.0)
                continue

            is_long = (direction.upper() == "BUY")
            trade_num += 1
            logging.info("Trade #%d (%s): %s %s size=%.2f | TP=%.2f pts (≈€%.2f), SL=%.2f pts",
                         trade_num, SCALP_STRATEGY, direction, epic, size, tp_pts, PER_TRADE_TARGET_EUR, sl_pts)

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
                logging.error("Deal not accepted: %s", status)
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
            except Exception:
                entry_level = float(last_mid) if last_mid is not None else None

            move_pts, exit_mid = trade_manager(
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

            pnl_eur = 0.0
            if move_pts is not None:
                pnl_eur = float(size) * (float(move_pts) / float(points_per_pip)) * float(pip_value_eur)

            # Record to ledger
            trade_rec = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "epic": epic,
                "direction": direction,
                "size": float(size),
                "currency": currency,
                "entry_level": float(entry_level) if entry_level is not None else None,
                "exit_level": float(exit_mid) if exit_mid is not None else None,
                "move_points": float(move_pts) if move_pts is not None else None,
                "tp_points": float(tp_pts),
                "sl_points": float(sl_pts),
                "pnl_eur": float(pnl_eur),
                "notes": "auto"
            }
            ledger.record_trade(trade_rec)

            # Update consecutive-loss counter
            if pnl_eur <= 0.0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0

            logging.info(
                "Trade exit. P&L=€%.2f | balance=€%.2f | today's net=€%.2f (goal +€%.2f / max loss -€%.2f) | consecLosses=%d/%d",
                pnl_eur, ledger.balance, ledger.day_net(), DAILY_TARGET_EUR, DAILY_MAX_LOSS_EUR,
                consecutive_losses, MAX_CONSECUTIVE_LOSSES
            )

            # Re-check guards immediately after a trade
            if ledger.day_net() >= DAILY_TARGET_EUR - 1e-6:
                logging.info("Daily profit target reached after trade.")
                break
            if ledger.day_net() <= -DAILY_MAX_LOSS_EUR + 1e-6:
                logging.info("Daily loss limit hit after trade — stopping for the day.")
                break
            if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logging.info("Max consecutive losses reached after trade — stopping for the day.")
                break

            # Optionally refresh instrument details (min distances etc.)
            try:
                details = ig.market_details(epic)
                size, tp_pts, sl_pts, currency, ok = compute_size_and_distances(
                    details=details,
                    target_eur=PER_TRADE_TARGET_EUR,
                    working_capital_eur=working_capital,  # working capital fixed for today
                    effective_leverage=EFFECTIVE_LEVERAGE,
                    margin_utilization=MARGIN_UTILIZATION,
                )
                if not ok or size <= 0:
                    logging.info("Sizing now exceeds budget; pausing trades for today.")
                    break
                logging.info("Adjusted sizing: size=%.2f TP=%.2f SL=%.2f", size, tp_pts, sl_pts)
            except Exception as e:
                logging.error("%s", e)

        logging.info("Target or guard reached, or stop requested. Tidying up…")
        close_all_positions(ig, epic=None)

    except Exception as e:
        logging.error("%s", e)
    finally:
        try:
            ig.logout()
        except Exception:
            pass
        logging.info("Logged out. Bye.")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=True)
    except Exception:
        pass
    main()
