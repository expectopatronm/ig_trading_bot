"""
sizing.py — Instrument selection and position sizing helpers.

Updates:
- Prefer the Germany 40 epic with the **smallest contractSize** (Mini/Micro first).
- Enforce exposure and margin caps relative to today's working capital.
- Clamp STOP_TO_LIMIT_MULTIPLIER to >= 1.0 (stop distance won't be smaller than TP).
"""
from __future__ import annotations

import logging
from typing import Dict, Any, Tuple

from config import STOP_TO_LIMIT_MULTIPLIER
from ig_client import IGRest


def _points_per_pip(one_pip_means: str) -> float:
    if not one_pip_means:
        return 1.0
    try:
        tok = str(one_pip_means).split()[0]
        return float(tok)
    except Exception:
        return 1.0


def _first_margin_rate(instr: Dict[str, Any]) -> float:
    bands = instr.get("marginDepositBands") or []
    if bands:
        try:
            return float(bands[0].get("margin", 5.0)) / 100.0
        except Exception:
            pass
    try:
        mf = float(instr.get("marginFactor"))
        if mf > 1.0:
            return mf / 100.0
        if 0.0 < mf <= 1.0:
            return mf
    except Exception:
        pass
    return 0.05


def _estimate_margin(price: float, size: float, contract_size: float, margin_rate: float) -> float:
    return float(price) * float(size) * float(contract_size) * float(margin_rate)


def _estimate_exposure(price: float, size: float, contract_size: float) -> float:
    return float(price) * float(size) * float(contract_size)


def choose_germany40_epic(ig: IGRest) -> Tuple[str, Dict[str, Any]]:
    terms = ["Germany 40", "Germany40", "DAX", "GER40", "DE40"]
    seen = set()
    candidates: list[tuple[float, str, Dict[str, Any]]] = []

    for t in terms:
        try:
            res = ig.search_markets(t)
        except Exception as e:
            logging.error("%s", e)
            continue
        for m in (res.get("markets") or []):
            epic = m.get("epic")
            if not epic or epic in seen:
                continue
            seen.add(epic)
            try:
                det = ig.market_details(epic)
            except Exception:
                continue
            instr = det.get("instrument", {}) or {}
            if (instr.get("type") or m.get("instrumentType") or "").upper() not in ("INDICES", "INDEX"):
                continue
            name = (instr.get("name") or m.get("instrumentName") or "").lower()
            if not any(k in name for k in ["germany 40", "dax", "ger40", "de40"]):
                continue
            try:
                csz = float(instr.get("contractSize") or 1.0)
            except Exception:
                csz = 1.0
            candidates.append((csz, epic, det))

    if not candidates:
        default_epic = "IX.D.DAX.IFMM.IP"  # region-dependent; best-effort fallback
        det = ig.market_details(default_epic)
        return default_epic, det

    candidates.sort(key=lambda x: (x[0], _first_margin_rate(x[2].get("instrument", {}))))
    _, best_epic, best_det = candidates[0]
    logging.info("Selected epic %s (contractSize=%s)", best_epic, best_det.get("instrument", {}).get("contractSize"))
    return best_epic, best_det


def compute_size_and_distances(
    details: Dict[str, Any],
    target_eur: float,
    working_capital_eur: float,
    effective_leverage: float = 5.0,
    margin_utilization: float = 1.0,
) -> Tuple[float, float, float, str, bool]:
    """
    Compute (size, limit_distance_points, stop_distance_points, currency, ok_to_trade).
    """
    instr = details.get("instrument", {}) or {}
    rules = details.get("dealingRules", {}) or {}
    snap = details.get("snapshot", {}) or {}

    ppp = _points_per_pip(instr.get("onePipMeans", "1"))
    try:
        pip_value_eur = float(instr.get("valueOfOnePip") or 1.0)
    except Exception:
        pip_value_eur = 1.0

    currency = "EUR"
    for c in instr.get("currencies", []) or []:
        if c.get("isDefault"):
            currency = c.get("code") or currency
            break

    min_limit = float((rules.get("minNormalStopOrLimitDistance", {}) or {}).get("value") or 0.1)
    min_stop = float((rules.get("minNormalStopOrLimitDistance", {}) or {}).get("value") or 0.1)
    min_size = float((rules.get("minDealSize", {}) or {}).get("value") or 0.1)
    max_size = float((rules.get("maxDealSize", {}) or {}).get("value") or 1e9)

    price = float(snap.get("offer") or snap.get("bid") or snap.get("mid") or 20000.0)
    contract_size = float(instr.get("contractSize") or 1.0)
    margin_rate = _first_margin_rate(instr)

    exposure_cap = max(0.0, float(working_capital_eur) * float(effective_leverage))
    margin_cap = max(0.0, float(working_capital_eur) * float(margin_utilization))

    def size_from_margin(cap: float) -> float:
        if cap <= 0:
            return 0.0
        den = price * contract_size * margin_rate
        return 0.0 if den <= 0 else cap / den

    def size_from_exposure(cap: float) -> float:
        if cap <= 0:
            return 0.0
        den = price * contract_size
        return 0.0 if den <= 0 else cap / den

    size_cap_margin = size_from_margin(margin_cap)
    size_cap_exposure = size_from_exposure(exposure_cap)
    max_affordable_size = max(0.0, min(size_cap_margin, size_cap_exposure, max_size))

    # Refuse to trade if even the minimum size breaches the caps
    min_size_margin = _estimate_margin(price, min_size, contract_size, margin_rate)
    min_size_exposure = _estimate_exposure(price, min_size, contract_size)
    if (min_size_margin > margin_cap + 1e-9) or (min_size_exposure > exposure_cap + 1e-9):
        logging.info(
            "Sizing blocked: min size exceeds budget | min_margin=€%.2f vs cap=€%.2f, min_exposure=€%.2f vs cap=€%.2f",
            min_size_margin, margin_cap, min_size_exposure, exposure_cap
        )
        return 0.0, 0.0, 0.0, currency, False

    # Choose size within caps but >= min_size
    size = min(max(min_size, max_affordable_size), max_affordable_size)

    # TP so that TP ≈ target_eur at this size
    pips_needed = max(1e-9, float(target_eur) / (pip_value_eur * size))
    points_needed = pips_needed * ppp
    limit_distance = max(min_limit, points_needed)

    # Clamp multiplier to >= 1.0 to avoid stop < limit distance
    mult = max(1.0, float(STOP_TO_LIMIT_MULTIPLIER))
    stop_distance = max(min_stop, limit_distance * mult)

    est_margin = _estimate_margin(price, size, contract_size, margin_rate)
    est_exposure = _estimate_exposure(price, size, contract_size)
    logging.info(
        "Sizing: size=%.2f | TP=%.2f pts | SL=%.2f pts | estMargin=€%.2f (cap €%.2f) | exposure=€%.2f (cap €%.2f)",
        size, limit_distance, stop_distance, est_margin, margin_cap, est_exposure, exposure_cap
    )

    return round(size, 2), round(limit_distance, 2), round(stop_distance, 2), currency, True
