#!/usr/bin/env python3
# main.py — IG REST DAX scalper (DEMO)
# - REST only (no streaming)
# - CFD vs Spreadbet aware (expiry "-" for CFD, "DFB" for spreadbet)
# - Token refresh on 401
# - Graceful shutdown closes open positions
# - Per-trade TP ≈ €1, repeats until ≈ €10 daily target
# - Smarter management: breakeven move + trailing stop, signal invalidation exit, volatility/spread gates

import os
import sys, time, json, signal, logging, threading, math
from typing import Dict, Any, Optional, Tuple, List

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

DEMO_BASE = "https://demo-api.ig.com/gateway/deal"

# ===== Strategy targets & risk =====
PER_TRADE_TARGET_EUR = 1.0
DAILY_TARGET_EUR = 10.0
STOP_TO_LIMIT_MULTIPLIER = 3.0  # SL distance = 3x TP distance (>= IG min stop)

# ===== Entry filters / management =====
EMA_PERIOD = 20  # for signal invalidation (1-min EMA)
ATR_PERIOD = 14  # ATR period in 1-min bars
ATR_MIN_THRESHOLD = 3.0  # points require this ATR to enter exit if it collapses below
SPREAD_MAX_POINTS = 3.0  # points skip entry if spread too wide exit if it spikes above
BREAKEVEN_TRIGGER_RATIO = 0.5  # activate trailing when move >= 50% of TP distance
BREAKEVEN_OFFSET_POINTS = 0.1  # tiny cushion past entry when moving stop to breakeven
TRAIL_DIST_ATR_MULT = 0.8  # trailing stop distance = 0.8 x ATR (clamped by IG min stop)
TRAIL_STEP_ATR_MULT = 0.3  # trailing increment step = 0.3 x ATR (>= MIN_TRAIL_STEP_POINTS)
MIN_TRAIL_STEP_POINTS = 0.1  # floor for trailing increment step (points)

# ===== Infra =====
POLL_POSITIONS_SEC = 2.0
RETRY_BACKOFF_SEC = 1.0

stop_event = threading.Event()


class IGRest:
    """Minimal IG REST API client focused on market, pricing, and OTC dealing calls.

    This client:
      - Manages authentication tokens (CST, X-SECURITY-TOKEN).
      - Automatically retries once on HTTP 401 using /session/refresh-token.
      - Exposes helpers for markets, prices, opening/closing positions, and updating stops.

    Attributes:
        api_key: Your IG API key.
        username: IG login identifier.
        password: IG login password.
        account_id: Preferred account id to use (optional).
        account_type: Resolved account type ("CFD" or "SPREADBET") after login.
        cst: Client security token returned by login.
        xst: Security token returned by login.
        s: Underlying requests.Session used for all HTTP calls.
    """

    def __init__(self, api_key: str, username: str, password: str, account_id: Optional[str] = None):
        """Initialize the REST client with credentials and optional account id.

        Args:
            api_key: IG REST API key.
            username: IG username / identifier.
            password: IG password.
            account_id: Optional preferred account id to select after login.
        """
        self.api_key = api_key
        self.username = username
        self.password = password
        self.account_id = account_id
        self.account_type = "CFD"
        self.cst = None
        self.xst = None
        self.s = requests.Session()

    # ----- low-level helpers -----

    def _headers(self, version: Optional[str] = None) -> Dict[str, str]:
        """Build default request headers including API key, tokens, and optional VERSION."""
        h = {
            "X-IG-API-KEY": self.api_key,
            "Accept": "application/json charset=UTF-8",
            "Content-Type": "application/json",
        }
        if self.cst:
            h["CST"] = self.cst
        if self.xst:
            h["X-SECURITY-TOKEN"] = self.xst
        if version:
            h["VERSION"] = version
        return h

    def _request(self, method: str, url: str, version: Optional[str] = None, **kwargs) -> requests.Response:
        """Send an HTTP request with IG headers and refresh tokens once on 401.

        If the first request returns 401, invokes /session/refresh-token and retries once.
        Returns the final Response (may be non-2xx)."""
        r = self.s.request(method, url, headers=self._headers(version), timeout=kwargs.pop("timeout", 20), **kwargs)
        if r.status_code == 401:
            try:
                logging.warning("401 on %s %s | body=%s", method, url, r.text[:300])
            except Exception:
                pass
            rr = self.s.post(f"{DEMO_BASE}/session/refresh-token", headers=self._headers("1"), timeout=10)
            if rr.status_code in (200, 201):
                self.cst = rr.headers.get("CST") or self.cst
                self.xst = rr.headers.get("X-SECURITY-TOKEN") or self.xst
                r = self.s.request(method, url, headers=self._headers(version), timeout=20, **kwargs)
        return r

    # ----- session -----

    def login(self) -> None:
        """Authenticate, store tokens, select default account, and resolve account type."""
        r = self.s.post(f"{DEMO_BASE}/session",
                        headers=self._headers("2"),
                        json={"identifier": self.username, "password": self.password},
                        timeout=20)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Login failed: HTTP {r.status_code} {r.text[:400]}")
        self.cst = r.headers.get("CST")
        self.xst = r.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.xst:
            raise RuntimeError("Missing CST/X-SECURITY-TOKEN after login")

        # Determine account id & type
        if not self.account_id:
            try:
                sess = self.s.get(f"{DEMO_BASE}/session", headers=self._headers("1"), timeout=10).json()
                self.account_id = sess.get("currentAccountId")
            except Exception:
                pass

        # Ensure preferred/default account is set (prevents certain 401s)
        if self.account_id:
            r2 = self.s.put(f"{DEMO_BASE}/session",
                            headers=self._headers("1"),
                            json={"accountId": self.account_id, "defaultAccount": True},
                            timeout=10)
            if r2.status_code not in (200, 204):
                logging.warning("Setting preferred account returned %s: %s", r2.status_code, r2.text[:200])

        # Get account type
        try:
            acct = self._request("GET", f"{DEMO_BASE}/accounts", "1")
            acct.raise_for_status()
            data = acct.json()
            for a in data.get("accounts", []):
                if a.get("accountId") == self.account_id:
                    self.account_type = a.get("accountType")
                    break
            logging.info("Active account type: %s", self.account_type or "?")
        except Exception as e:
            logging.warning("Could not determine account type: %s", e)

    def logout(self) -> None:
        """End the current session (best-effort) by calling DELETE /session."""
        try:
            self.s.delete(f"{DEMO_BASE}/session", headers=self._headers("1"), timeout=10)
        except Exception:
            pass

    # ----- markets & prices -----

    def search_markets(self, term: str) -> Dict[str, Any]:
        """Search for markets using a free-text term."""
        r = self._request("GET", f"{DEMO_BASE}/markets?searchTerm={requests.utils.quote(term)}", "1")
        if r.status_code >= 400:
            raise RuntimeError(f"search_markets failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def market_details(self, epic: str) -> Dict[str, Any]:
        """Retrieve detailed instrument metadata and snapshot for a market epic."""
        r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "4")
        if r.status_code == 404:
            r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "3")
        if r.status_code >= 400:
            raise RuntimeError(f"market_details failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def recent_prices(self, epic: str, resolution: str = "MINUTE", num_points: int = 3) -> Dict[str, Any]:
        """Fetch a small window of recent prices for a market (candles)."""
        r = self._request("GET", f"{DEMO_BASE}/prices/{epic}/{resolution}/{num_points}", "2")
        if r.status_code >= 400:
            raise RuntimeError(f"recent_prices failed: {r.status_code} {r.text[:400]}")
        return r.json()

    # ----- dealing -----

    def open_market_position(
            self,
            epic: str,
            direction: str,
            size: float,
            currency: str,
            limit_distance_points: float,
            stop_distance_points: Optional[float],
    ) -> Tuple[str, Dict[str, Any]]:
        """Open a MARKET position with attached limit/stop distances.

        Uses FILL_OR_KILL and forceOpen=True (except when netting off elsewhere).
        Expiry is '-' for CFD cash indices and 'DFB' for spread bets.
        """
        expiry = "-" if (self.account_type or "CFD").upper() == "CFD" else "DFB"
        payload = {
            "epic": epic,
            "expiry": expiry,  # CFD cash indices use "-" (undated) spreadbet uses "DFB"
            "direction": direction.upper(),
            "size": float(size),
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "forceOpen": True,
            "guaranteedStop": False,
            "currencyCode": currency,
            "limitDistance": float(limit_distance_points),
        }
        if stop_distance_points and stop_distance_points > 0:
            payload["stopDistance"] = float(stop_distance_points)

        r = self._request("POST", f"{DEMO_BASE}/positions/otc", "2", json=payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Open position failed: HTTP {r.status_code} {r.text[:400]}")
        deal_ref = r.json().get("dealReference")
        confirm = self.deal_confirm(deal_ref)
        return deal_ref, confirm

    def update_position(self, deal_id: str, *,
                        limit_level: float | None = None,
                        stop_level: float | None = None,
                        trailing_stop: bool | None = None,
                        trailing_stop_distance: float | None = None,
                        trailing_stop_increment: float | None = None) -> str:
        """Update an existing OTC position (e.g., set breakeven & trailing stop)."""
        payload: Dict[str, Any] = {}
        if limit_level is not None:
            payload["limitLevel"] = float(limit_level)
        if trailing_stop is not None:
            payload["trailingStop"] = bool(trailing_stop)
        if trailing_stop:
            if trailing_stop_distance is None or trailing_stop_increment is None or stop_level is None:
                raise ValueError("Trailing stop requires stop_level, trailing_stop_distance, trailing_stop_increment")
            payload["trailingStopDistance"] = float(trailing_stop_distance)
            payload["trailingStopIncrement"] = float(trailing_stop_increment)
            payload["stopLevel"] = float(stop_level)
        elif stop_level is not None:
            payload["stopLevel"] = float(stop_level)

        r = self._request("PUT", f"{DEMO_BASE}/positions/otc/{deal_id}", "2", json=payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Update position failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json().get("dealReference", "")
        except Exception:
            return ""

    def deal_confirm(self, deal_reference: str) -> Dict[str, Any]:
        """Retrieve a structured confirmation for a prior dealReference."""
        r = self._request("GET", f"{DEMO_BASE}/confirms/{deal_reference}", "1")
        r.raise_for_status()
        return r.json()

    def list_positions(self) -> Dict[str, Any]:
        """List all open positions on the active account."""
        r = self._request("GET", f"{DEMO_BASE}/positions", "2")
        r.raise_for_status()
        return r.json()

    def close_position_market(self, deal_id: str, direction_open: str, size: float,
                              epic: str | None = None, expiry: str | None = None,
                              currency: str | None = None) -> str:
        """Close an open position using the most reliable method (prefer net-off)."""
        opposite = "SELL" if (direction_open or "").upper() == "BUY" else "BUY"

        # --- Prefer NET-OFF first if we know epic/currency ---
        if epic and currency:
            expiry = expiry or ("-" if (self.account_type or "CFD").upper() == "CFD" else "DFB")
            rev = {
                "epic": epic,
                "expiry": expiry,
                "direction": opposite,
                "size": float(size),
                "orderType": "MARKET",
                "timeInForce": "FILL_OR_KILL",
                "forceOpen": False,  # netting off closes exposure
                "currencyCode": currency
            }
            r = self._request("POST", f"{DEMO_BASE}/positions/otc", "2", json=rev)
            if r.status_code in (200, 201):
                try:
                    return r.json().get("dealReference", "")
                except Exception:
                    return ""
            try:
                logging.warning("Net-off failed %s: %s", r.status_code, r.json().get("errorCode"))
            except Exception:
                logging.warning("Net-off failed %s", r.status_code)

        # --- Official DELETE with JSON body ---
        payload = {
            "dealId": deal_id,
            "direction": opposite,
            "size": float(size),
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
        }
        r = self._request("DELETE", f"{DEMO_BASE}/positions/otc", "1", json=payload)
        if r.status_code in (200, 201):
            try:
                return r.json().get("dealReference", "")
            except Exception:
                return ""

        # --- Method-override POST as last resort ---
        hdrs = self._headers("1")
        hdrs["X-HTTP-Method-Override"] = "DELETE"
        r2 = self.s.post(f"{DEMO_BASE}/positions/otc", headers=hdrs, json=payload, timeout=20)
        if r2.status_code in (200, 201):
            try:
                return r2.json().get("dealReference", "")
            except Exception:
                return ""

        # Bubble up the most informative error
        err = None
        try:
            err = r.json().get("errorCode")
        except Exception:
            pass
        raise RuntimeError(f"Close position failed: HTTP {r.status_code} {err or r.text[:200]}")


# ===== helpers: sizing / indicators =====

def _points_per_pip(one_pip_means: str) -> float:
    """Parse the instrument's 'onePipMeans' text and return points per pip."""
    if not one_pip_means:
        return 1.0
    try:
        return float(one_pip_means.strip().split()[0])
    except Exception as e:
        logging.info(f"Exception parsing onePipMeans '{one_pip_means}': {e}")
        return 1.0


def ema(values: List[float], period: int) -> float:
    """Compute EMA over a list (last value returned)."""
    if not values:
        return float("nan")
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def ema_of_closes(bars: List[Dict[str, Any]], period: int) -> float:
    """EMA of mid-closes from IG price bars."""
    closes = []
    for b in bars:
        cp = b.get("closePrice", {})
        mid = cp.get("mid")
        if mid is None:
            bid = cp.get("bid")
            ask = cp.get("ask")
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
        if mid is not None:
            closes.append(float(mid))
    if len(closes) == 0:
        return float("nan")
    return ema(closes, period)


def _bar_mid(x: Dict[str, Any], key: str) -> Optional[float]:
    """Get mid of a price component (highPrice/lowPrice/closePrice)."""
    d = x.get(key, {})
    mid = d.get("mid")
    if mid is None:
        bid = d.get("bid")
        ask = d.get("ask")
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
    return float(mid) if mid is not None else None


def compute_atr_points(bars: List[Dict[str, Any]], period: int = 14) -> float:
    """Classic ATR in points using mid highs/lows and previous close."""
    if len(bars) < period + 1:
        return float("nan")
    trs: List[float] = []
    prev_close = _bar_mid(bars[0], "closePrice")
    for b in bars[1:]:
        high = _bar_mid(b, "highPrice")
        low = _bar_mid(b, "lowPrice")
        close = _bar_mid(b, "closePrice")
        if high is None or low is None or prev_close is None:
            prev_close = close
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if len(trs) < period:
        return float("nan")
    # Simple moving average of last `period` TRs
    return sum(trs[-period:]) / float(period)


def choose_germany40_epic(ig: IGRest) -> Tuple[str, Dict[str, Any]]:
    """Select a Germany 40 market epic that matches the account's unit type.

    Searches several terms, filters to INDICES with the expected unit
    ('CONTRACTS' for CFD, 'AMOUNT' for spread bet), prefers EUR markets,
    and returns the candidate with the smallest pip value (often the mini)."""
    want_unit = "CONTRACTS" if (ig.account_type or "CFD").upper() == "CFD" else "AMOUNT"
    markets = []
    for term in ("germany 40", "dax", "germany40"):
        try:
            res = ig.search_markets(term)
            markets.extend(res.get("markets", []))
        except Exception:
            pass
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
        except Exception:
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

    # start with size=1, derive points for ≈ target_eur
    size = max(1.0, min_size)
    pips_needed = max(1e-9, target_eur / (pip_value_eur * size))
    points_needed = pips_needed * ppp
    if points_needed < min_limit:
        pips_needed = max(1e-9, (min_limit / ppp))
        size = max(min_size, target_eur / (pip_value_eur * pips_needed))
    limit_distance = max(min_limit, points_needed)
    stop_distance = max(min_stop, limit_distance * STOP_TO_LIMIT_MULTIPLIER)

    # crude margin estimate & scale to budget
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


# ===== micro strategy & lifecycle =====

def momentum_direction(ig: IGRest, epic: str) -> str:
    """Tiny momentum heuristic based on last two 1-minute candle closes."""
    try:
        pr = ig.recent_prices(epic, "MINUTE", 3).get("prices", [])
        if len(pr) >= 2:
            def mid(px):
                cp = px.get("closePrice", {})
                m = cp.get("mid")
                if m is None:
                    b = cp.get("bid")
                    a = cp.get("ask")
                    if b is not None and a is not None:
                        m = (b + a) / 2.0
                return m

            return "BUY" if float(mid(pr[-1])) >= float(mid(pr[-2])) else "SELL"
    except Exception:
        pass
    return "BUY"


def latest_mid_and_spread(bars: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Return (mid_close, spread_points) from last bar."""
    if not bars:
        return None, None
    cp = bars[-1].get("closePrice", {})
    bid, ask, mid = cp.get("bid"), cp.get("ask"), cp.get("mid")
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    spread = None
    if bid is not None and ask is not None:
        spread = ask - bid
    return (float(mid) if mid is not None else None,
            float(spread) if spread is not None else None)


def trade_manager(ig: IGRest, deal_id: str, epic: str, currency: str, is_long: bool,
                  entry_level: float, tp_pts: float, ig_min_stop_points: float) -> bool:
    """Manage an open trade until it closes. Returns True if exit likely >= ~€1, else False.

    Logic:
      - If move >= BREAKEVEN_TRIGGER_RATIO * TP: set breakeven + activate trailing stop
        with ATR-scaled distance/increment (clamped by IG min stop).
      - Exit immediately if signal invalidates (close < EMA for long, > EMA for short).
      - Exit if ATR collapses below threshold or spread spikes.
      - Polls for closure (TP/SL/trailing) every few seconds.
    """
    trailing_activated = False
    approx_favourable = False  # set True if we reached meaningful favourable move

    while not stop_event.is_set():
        # If position is gone, we are done
        positions = ig.list_positions()
        pos_row = None
        for p in positions.get("positions", []):
            if p.get("position", {}).get("dealId") == deal_id:
                pos_row = p
                break
        if pos_row is None:
            # infer favourability from last price move if we can
            bars = ig.recent_prices(epic, "MINUTE", 2).get("prices", [])
            last_mid, _ = latest_mid_and_spread(bars)
            if last_mid is not None:
                move_pts = (last_mid - entry_level) if is_long else (entry_level - last_mid)
                if move_pts >= (tp_pts * 0.8) or trailing_activated:
                    approx_favourable = True
            return approx_favourable

        # Get fresh bars to compute ATR, EMA, spread
        bars = ig.recent_prices(epic, "MINUTE", max(ATR_PERIOD + 2, 30)).get("prices", [])
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

        # Activate trailing at ~half TP set stop to breakeven + tiny cushion
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
                approx_favourable = True  # we got a decent push
                logging.info("Trailing activated: dist=%.2f, step=%.2f, stopLevel≈%.2f",
                             trail_dist, trail_step, breakeven)
            except Exception as e:
                logging.warning("Failed to activate trailing: %s", e)

        # Signal invalidation: opposite side of EMA20
        if (is_long and last_mid < ema20) or ((not is_long) and last_mid > ema20):
            try:
                # Net-off for reliability
                position = pos_row["position"]
                market = pos_row.get("market", {})
                ig.close_position_market(
                    deal_id,
                    position["direction"],
                    float(position["size"]),
                    epic=position.get("epic") or market.get("epic"),
                    expiry=position.get("expiry") or market.get("expiry"),
                    currency=position.get("currency") or market.get("currency")
                )
            except Exception as e:
                logging.warning("Invalidation close failed: %s", e)
            return approx_favourable

        # Volatility/spread exit
        if (atr < ATR_MIN_THRESHOLD) or (spread is not None and spread > SPREAD_MAX_POINTS):
            try:
                position = pos_row["position"]
                market = pos_row.get("market", {})
                ig.close_position_market(
                    deal_id,
                    position["direction"],
                    float(position["size"]),
                    epic=position.get("epic") or market.get("epic"),
                    expiry=position.get("expiry") or market.get("expiry"),
                    currency=position.get("currency") or market.get("currency")
                )
            except Exception as e:
                logging.warning("Vol/Spread exit failed: %s", e)
            return approx_favourable

        time.sleep(POLL_POSITIONS_SEC)


def close_all_positions(ig: IGRest, epic: str | None = None) -> None:
    """Attempt to close all (or all matching) open positions at market."""
    try:
        pos = ig.list_positions()
        for p in pos.get("positions", []):
            position = p.get("position", {}) or {}
            market = p.get("market", {}) or {}
            deal_id = position.get("dealId")
            direction = position.get("direction")
            size = float(position.get("size", 0) or 0)
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
                logging.warning("Failed closing %s: %s", deal_id, e)
    except Exception as e:
        logging.warning("List positions during shutdown failed: %s", e)


# ===== main loop =====

def main():
    """Run the demo scalper until target is reached or a stop signal arrives.

    Flow:
        - Login and select a Germany 40 market matching the account type.
        - Compute size/TP/SL to target ≈ €1 per trade within a margin budget.
        - Entry gating: only trade when ATR >= threshold and spread <= max.
        - Place a MARKET order (simple momentum bias).
        - Manage open trade via breakeven + trailing + invalidation/volatility exits (no hard timeout).
        - Re-compute sizing periodically (rules/price may change).
        - On exit or signal, attempt to close any remaining positions and logout.
    """
    api_key = os.environ.get("API_KEY")
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    account_id = os.environ.get("ACCOUNT_ID")

    if not all([api_key, username, password]):
        print("Set API_KEY, USERNAME, PASSWORD (and ACCOUNT_ID) as env vars.", file=sys.stderr)
        sys.exit(2)

    ig = IGRest(api_key, username, password, account_id)

    def handle_signal(sig, frame):
        """Signal handler to stop the run, close positions, logout, and exit."""
        logging.warning("Signal %s received: closing positions and shutting down…", sig)
        stop_event.set()
        try:
            close_all_positions(ig, epic=None)
        finally:
            ig.logout()
            sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        ig.login()

        # Pick a Germany 40 market that matches your account type (CFD -> unit CONTRACTS)
        epic, details = choose_germany40_epic(ig)

        # Compute size + attached orders to target ≈ €1 per trade, within ~€500 margin
        size, tp_pts, sl_pts, currency = compute_size_and_distances(details, PER_TRADE_TARGET_EUR, 500.0)
        logging.info("Initial sizing: size=%.2f, TP=%.2f pts, SL=%.2f pts, currency=%s", size, tp_pts, sl_pts, currency)

        # Pull IG min stop once for trailing clamping
        ig_min_stop_points = float(
            details.get("dealingRules", {}).get("minNormalStopOrLimitDistance", {}).get("value", 0.1))

        realized_approx = 0.0
        trade_num = 0

        while (realized_approx + 1e-6) < DAILY_TARGET_EUR and not stop_event.is_set():
            # Entry gating: ATR and spread must be reasonable
            bars = ig.recent_prices(epic, "MINUTE", max(ATR_PERIOD + 2, 30)).get("prices", [])
            if not bars:
                time.sleep(2.0)
                continue
            atr = compute_atr_points(bars, ATR_PERIOD)
            last_mid, spread = latest_mid_and_spread(bars)
            if (math.isnan(atr) or last_mid is None or
                    atr < ATR_MIN_THRESHOLD or
                    (spread is not None and spread > SPREAD_MAX_POINTS)):
                logging.info("Skip entry: ATR=%.2f (min %.2f), spread=%s (max %.2f)",
                             atr if not math.isnan(atr) else float("nan"),
                             ATR_MIN_THRESHOLD,
                             f"{spread:.2f}" if spread is not None else "n/a",
                             SPREAD_MAX_POINTS)
                time.sleep(5.0)
                continue

            # Micro-bias
            direction = momentum_direction(ig, epic)
            is_long = (direction.upper() == "BUY")
            trade_num += 1
            logging.info("Trade #%d: %s %s size=%.2f | TP=%.2f pts (≈€%.2f), SL=%.2f pts",
                         trade_num, direction, epic, size, tp_pts, PER_TRADE_TARGET_EUR, sl_pts)

            # Open
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
                logging.error("Open failed: %s (retrying after backoff)", e)
                time.sleep(RETRY_BACKOFF_SEC)
                continue

            status = (confirm.get("dealStatus") or confirm.get("status") or "").upper()
            deal_id = confirm.get("dealId")
            if status not in ("ACCEPTED", "OPENED", "FILLED") or not deal_id:
                logging.error("Deal not accepted: %s | confirm=%s", status, json.dumps(confirm)[:300])
                time.sleep(RETRY_BACKOFF_SEC)
                continue

            # Get entry level & manage trade until it closes
            try:
                pos = ig.list_positions()
                entry_level = None
                for p in pos.get("positions", []):
                    if p.get("position", {}).get("dealId") == deal_id:
                        entry_level = float(p["position"]["level"])
                        break
                if entry_level is None:
                    # fallback: use last mid as a rough stand-in
                    entry_level = float(last_mid)
            except Exception:
                entry_level = float(last_mid) if last_mid is not None else None

            favourable = trade_manager(
                ig=ig,
                deal_id=deal_id,
                epic=epic,
                currency=currency,
                is_long=is_long,
                entry_level=entry_level,
                tp_pts=tp_pts,
                ig_min_stop_points=ig_min_stop_points
            )

            if favourable:
                realized_approx += PER_TRADE_TARGET_EUR
                logging.info("Favourable exit. Session realized ≈ €%.2f / €%.2f target",
                             realized_approx, DAILY_TARGET_EUR)
            else:
                logging.info("Unfavourable/neutral exit not adding to €1-per-trade goal.")

            # Re-check sizing in case rules/price changed
            try:
                details = ig.market_details(epic)
                new_size, new_tp, new_sl, _ = compute_size_and_distances(details, PER_TRADE_TARGET_EUR, 500.0)
                if (abs(new_size - size) > 1e-6) or (abs(new_tp - tp_pts) > 1e-6):
                    size, tp_pts, sl_pts = new_size, new_tp, new_sl
                    logging.info("Adjusted sizing: size=%.2f, TP=%.2f, SL=%.2f", size, tp_pts, sl_pts)
            except Exception:
                pass

        logging.info("Target reached or stop requested. Tidying up…")
        close_all_positions(ig, epic=None)

    finally:
        ig.logout()
        logging.info("Logged out. Bye.")


if __name__ == "__main__":
    # Optional: load .env for API_KEY / USERNAME / PASSWORD / ACCOUNT_ID
    try:
        from dotenv import load_dotenv

        load_dotenv(".env", override=True)
    except Exception:
        pass
    main()
