"""
risk.py — Trade management: breakeven move + ATR trailing, EMA invalidation, spread/ATR gates.

UPDATED:
- Returns an approximate move in *points* and an approximate exit price instead of a True/False flag.
  Positive move = favourable, negative move = loss. Caller converts this to EUR using instrument pip info.
"""
import logging
import math
import time
from typing import Dict, Any, Tuple, Optional

from config import (
    POLL_POSITIONS_SEC, ATR_PERIOD, EMA_PERIOD, ATR_MIN_THRESHOLD, SPREAD_MAX_POINTS,
    BREAKEVEN_TRIGGER_RATIO, TRAIL_DIST_ATR_MULT, TRAIL_STEP_ATR_MULT,
    MIN_TRAIL_STEP_POINTS, BREAKEVEN_OFFSET_POINTS
)
from indicators import compute_atr_points, ema_of_closes, latest_mid_and_spread


def trade_manager(ig, deal_id: str, epic: str, currency: str, is_long: bool,
                  entry_level: float, tp_pts: float, ig_min_stop_points: float,
                  stop_event) -> Tuple[Optional[float], Optional[float]]:
    """
    Manage an open trade until it closes.
    Returns (approx_move_points, approx_exit_mid).

    Notes:
    - After +50% of TP distance, move stop to tiny positive breakeven and enable ATR-trailing.
    - Exit early on EMA(20) invalidation or if ATR/spread gates deteriorate.
    - Polls until position disappears.
    """
    trailing_activated = False

    while not stop_event.is_set():
        try:
            positions: Dict[str, Any] = ig.list_positions()
        except Exception as e:
            logging.error("%s", e)
            time.sleep(POLL_POSITIONS_SEC)
            continue

        pos_row = None
        for p in positions.get("positions", []):
            if p.get("position", {}).get("dealId") == deal_id:
                pos_row = p
                break
        if pos_row is None:
            # Position has disappeared (hit TP/SL or was closed by our earlier request).
            try:
                bars = ig.recent_prices(epic, "MINUTE", 2).get("prices", [])
                last_mid, _ = latest_mid_and_spread(bars)
                if last_mid is None:
                    return None, None
                move_pts = (last_mid - entry_level) if is_long else (entry_level - last_mid)
                return float(move_pts), float(last_mid)
            except Exception as e:
                logging.error("%s", e)
                return None, None

        # We still have an open position — analyse current context
        try:
            bars = ig.recent_prices(epic, "MINUTE", max(ATR_PERIOD + 2, 30)).get("prices", [])
        except Exception as e:
            logging.error("%s", e)
            time.sleep(POLL_POSITIONS_SEC)
            continue

        if len(bars) == 0:
            time.sleep(POLL_POSITIONS_SEC)
            continue

        atr = compute_atr_points(bars, ATR_PERIOD)
        ema20 = ema_of_closes(bars, EMA_PERIOD)
        last_mid, spread = latest_mid_and_spread(bars)
        if last_mid is None or math.isnan(atr) or math.isnan(ema20):
            time.sleep(POLL_POSITIONS_SEC)
            continue

        move_pts = (last_mid - entry_level) if is_long else (entry_level - last_mid)

        # Breakeven + ATR trailing after partial progress
        if (not trailing_activated) and move_pts >= (tp_pts * BREAKEVEN_TRIGGER_RATIO):
            trail_dist = max(atr * TRAIL_DIST_ATR_MULT, ig_min_stop_points)
            trail_step = max(atr * TRAIL_STEP_ATR_MULT, MIN_TRAIL_STEP_POINTS)
            breakeven = entry_level + (BREAKEVEN_OFFSET_POINTS if is_long else -BREAKEVEN_OFFSET_POINTS)
            try:
                ig.update_position(
                    deal_id,
                    trailing_stop=True,
                    trailing_stop_distance=round(trail_dist, 2),
                    trailing_stop_increment=round(trail_step, 2),
                    stop_level=round(breakeven, 2)
                )
                trailing_activated = True
                logging.info("Trailing activated: dist=%.2f, step=%.2f, stopLevel≈%.2f",
                             trail_dist, trail_step, breakeven)
            except Exception as e:
                logging.error("%s", e)

        # EMA invalidation close
        if (is_long and last_mid < ema20) or ((not is_long) and last_mid > ema20):
            try:
                position = pos_row["position"]; market = pos_row.get("market", {})
                ig.close_position_market(
                    deal_id, position["direction"], float(position["size"]),
                    epic=position.get("epic") or market.get("epic"),
                    expiry=position.get("expiry") or market.get("expiry"),
                    currency=position.get("currency") or market.get("currency")
                )
            except Exception as e:
                logging.error("%s", e)
            # Return the move at the time we gave the close instruction
            return float(move_pts), float(last_mid)

        # Vol/spread deterioration close
        if (atr < ATR_MIN_THRESHOLD) or (spread is not None and spread > SPREAD_MAX_POINTS):
            try:
                position = pos_row["position"]; market = pos_row.get("market", {})
                ig.close_position_market(
                    deal_id, position["direction"], float(position["size"]),
                    epic=position.get("epic") or market.get("epic"),
                    expiry=position.get("expiry") or market.get("expiry"),
                    currency=position.get("currency") or market.get("currency")
                )
            except Exception as e:
                logging.error("%s", e)
            return float(move_pts), float(last_mid)

        time.sleep(POLL_POSITIONS_SEC)
