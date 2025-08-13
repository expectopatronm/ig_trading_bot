"""
sizing.py — Instrument selection and position sizing helpers.
Changes rarely; safe to keep separate.
"""

import logging
from typing import Dict, Any, Tuple

from config import STOP_TO_LIMIT_MULTIPLIER
from ig_client import IGRest


def _points_per_pip(one_pip_means: str) -> float:
    if not one_pip_means:
        return 1.0
    try:
        return float(one_pip_means.strip().split()[0])
    except Exception as e:
        logging.error("%s", e)
        return 1.0


def choose_germany40_epic(ig: IGRest) -> Tuple[str, Dict[str, Any]]:
    """Select a Germany 40 market epic that matches the account's unit type."""
    want_unit = "CONTRACTS" if (ig.account_type or "CFD").upper() == "CFD" else "AMOUNT"
    markets = []
    for term in ("germany 40", "dax", "germany40"):
        try:
            res = ig.search_markets(term)
            markets.extend(res.get("markets", []))
        except Exception as e:
            logging.error("%s", e)
    seen = set()
    candidates = []
    for m in markets:
        epic = m.get("epic")
        if not epic or epic in seen:
            continue
        seen.add(epic)
        try:
            det = ig.market_details(epic)
            instr = det.get("instrument", {})
            if instr.get("type") != "INDICES":
                continue
            if instr.get("unit") != want_unit:
                continue
            eur_ok = any(c.get("code") == "EUR" for c in instr.get("currencies", []))
            if not eur_ok:
                continue
            pip_val = float(instr.get("valueOfOnePip"))
            status = (det.get("snapshot", {}) or {}).get("marketStatus", "UNKNOWN")
            candidates.append((epic, pip_val, status, det))
        except Exception as e:
            logging.error("%s", e)
            continue

    if not candidates:
        raise RuntimeError(f"No Germany 40 market found for unit={want_unit} (account type {ig.account_type}).")

    candidates.sort(key=lambda x: x[1])  # smallest pip value first
    epic, _, status, details = candidates[0]
    logging.info("Chosen EPIC %s (status=%s, unit=%s)", epic, status, details["instrument"]["unit"])
    return epic, details


def compute_size_and_distances(details: Dict[str, Any], target_eur: float, max_margin_eur: float) -> Tuple[
    float, float, float, str]:
    """Compute deal size, TP/SL distances (points), and currency for ~target P&L."""
    instr = details["instrument"]
    rules = details.get("dealingRules", {})

    ppp = _points_per_pip(instr.get("onePipMeans", "1"))
    pip_value_eur = float(instr.get("valueOfOnePip"))
    currency = next(
        (c["code"] for c in instr.get("currencies", []) if c.get("isDefault")),
        (instr.get("currencies", [{}])[0].get("code", "EUR") if instr.get("currencies") else "EUR")
    )

    min_limit = float(rules.get("minNormalStopOrLimitDistance", {}).get("value", 0.1))
    min_stop = float(rules.get("minNormalStopOrLimitDistance", {}).get("value", 0.1))
    min_size = float(rules.get("minDealSize", {}).get("value", 0.1))

    size = max(1.0, min_size)
    pips_needed = max(1e-9, target_eur / (pip_value_eur * size))
    points_needed = pips_needed * ppp
    if points_needed < min_limit:
        pips_needed = max(1e-9, (min_limit / ppp))
        size = max(min_size, target_eur / (pip_value_eur * pips_needed))
    limit_distance = max(min_limit, points_needed)
    stop_distance = max(min_stop, limit_distance * STOP_TO_LIMIT_MULTIPLIER)

    price = float((details.get("snapshot", {}) or {}).get("offer") or 20000.0)
    contract_size = float(instr.get("contractSize", "1") or 1)
    bands = instr.get("marginDepositBands", [])
    margin_pct = float((bands[0].get("margin") if bands else 5.0))
    est_margin = price * size * contract_size * (margin_pct / 100.0)
    if est_margin > max_margin_eur and est_margin > 0:
        scale = max_margin_eur / est_margin
        size = max(min_size, size * scale)
        pips_needed = max(1e-9, target_eur / (pip_value_eur * size))
        points_needed = pips_needed * ppp
        limit_distance = max(min_limit, points_needed)
        stop_distance = max(min_stop, limit_distance * STOP_TO_LIMIT_MULTIPLIER)

    return round(size, 2), round(limit_distance, 2), round(stop_distance, 2), currency
