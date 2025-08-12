#!/usr/bin/env python3
# main.py — IG REST DAX scalper (DEMO)
# - REST only (no streaming)
# - CFD vs Spreadbet aware (expiry "-" for CFD, "DFB" for spreadbet)
# - Token refresh on 401
# - Graceful shutdown closes open positions
# - Per-trade TP ≈ €1, repeats until ≈ €10 daily target
import os
import sys, time, json, signal, logging, threading
from typing import Dict, Any, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

DEMO_BASE = "https://demo-api.ig.com/gateway/deal"

# Strategy targets
PER_TRADE_TARGET_EUR = 1.0
DAILY_TARGET_EUR = 10.0
MAX_HOLD_SECONDS = 300           # close manually if TP not hit within 5 minutes
STOP_TO_LIMIT_MULTIPLIER = 3.0   # SL distance = 3x TP distance (rounded & >= min stop)

POLL_POSITIONS_SEC = 2.0
RETRY_BACKOFF_SEC = 1.0

stop_event = threading.Event()

class IGRest:
    """Minimal IG REST API client focused on market, pricing, and OTC dealing calls.

    This client:
      - Manages authentication tokens (CST, X-SECURITY-TOKEN).
      - Automatically retries once on HTTP 401 using /session/refresh-token.
      - Exposes helpers for markets, prices, and opening/closing positions.

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
        """Build default request headers including API key, tokens, and optional VERSION.

        Args:
            version: Optional IG API VERSION header value required by some endpoints.

        Returns:
            A dict of HTTP headers appropriate for IG REST calls.
        """
        h = {
            "X-IG-API-KEY": self.api_key,
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json",
        }
        if self.cst: h["CST"] = self.cst
        if self.xst: h["X-SECURITY-TOKEN"] = self.xst
        if version:  h["VERSION"] = version
        return h

    def _request(self, method: str, url: str, version: Optional[str] = None, **kwargs) -> requests.Response:
        """Send an HTTP request with IG headers and refresh tokens once on 401.

        If the first request returns 401, this method invokes /session/refresh-token
        (VERSION 1) and retries the original request once.

        Args:
            method: HTTP method (e.g., "GET", "POST", "DELETE").
            url: Fully qualified IG REST URL.
            version: Optional IG API VERSION header.
            **kwargs: Forwarded to requests.Session.request (e.g., json, data, timeout).

        Returns:
            The final requests.Response (may be non-2xx). No exceptions are raised here.
        """
        r = self.s.request(method, url, headers=self._headers(version), timeout=kwargs.pop("timeout", 20), **kwargs)
        if r.status_code == 401:
            try:
                logging.warning("401 on %s %s | body=%s", method, url, r.text[:300])
            except Exception:
                pass
            # Refresh tokens then retry once
            rr = self.s.post(f"{DEMO_BASE}/session/refresh-token", headers=self._headers("1"), timeout=10)
            if rr.status_code in (200, 201):
                self.cst = rr.headers.get("CST") or self.cst
                self.xst = rr.headers.get("X-SECURITY-TOKEN") or self.xst
                r = self.s.request(method, url, headers=self._headers(version), timeout=20, **kwargs)
        return r

    # ----- session -----

    def login(self) -> None:
        """Authenticate, store tokens, select default account, and resolve account type.

        Performs:
            - POST /session (VERSION 2) with identifier/password.
            - Optional GET/PUT to select preferred account to avoid some 401s.
            - GET /accounts to discover the active account type.

        Raises:
            RuntimeError: If authentication fails or tokens are missing.
        """
        # POST /session (VERSION 2/3). Returns session tokens (CST, X-SECURITY-TOKEN).
        r = self.s.post(f"{DEMO_BASE}/session",
                        headers=self._headers("2"),
                        data=json.dumps({"identifier": self.username, "password": self.password}),
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
                            data=json.dumps({"accountId": self.account_id, "defaultAccount": True}),
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
        """Search for markets using a free-text term.

        Args:
            term: Case-insensitive search term (e.g., "dax", "germany 40").

        Returns:
            Parsed JSON response from GET /markets?searchTerm=...

        Raises:
            RuntimeError: If the HTTP response has status >= 400.
        """
        r = self._request("GET", f"{DEMO_BASE}/markets?searchTerm={requests.utils.quote(term)}", "1")
        if r.status_code >= 400:
            raise RuntimeError(f"search_markets failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def market_details(self, epic: str) -> Dict[str, Any]:
        """Retrieve detailed instrument metadata and snapshot for a market epic.

        Tries VERSION 4, then falls back to VERSION 3 if needed.

        Args:
            epic: IG market epic (e.g., "IX.D.DAX.IFD.IP").

        Returns:
            Parsed JSON from GET /markets/{epic}.

        Raises:
            RuntimeError: If both attempts fail with HTTP status >= 400.
        """
        r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "4")
        if r.status_code == 404:
            r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "3")
        if r.status_code >= 400:
            raise RuntimeError(f"market_details failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def recent_prices(self, epic: str, resolution: str = "MINUTE", num_points: int = 3) -> Dict[str, Any]:
        """Fetch a small window of recent prices for a market.

        Args:
            epic: IG market epic.
            resolution: Candle resolution (e.g., 'MINUTE', 'HOUR').
            num_points: Number of data points to request.

        Returns:
            Parsed JSON from GET /prices/{epic}/{resolution}/{num_points}.

        Raises:
            RuntimeError: If the HTTP response has status >= 400.
        """
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

        Args:
            epic: Market epic to trade.
            direction: 'BUY' or 'SELL'.
            size: Deal size in instrument units (CONTRACTS or AMOUNT).
            currency: Currency code for the order (e.g., 'EUR').
            limit_distance_points: Take-profit distance in *points*.
            stop_distance_points: Optional stop-loss distance in *points*.

        Returns:
            Tuple (deal_reference, confirmation_json) where confirmation_json is the
            parsed result of GET /confirms/{dealReference}.

        Raises:
            RuntimeError: If the initial POST fails with non-2xx status.
        """
        expiry = "-" if (self.account_type or "CFD").upper() == "CFD" else "DFB"
        payload = {
            "epic": epic,
            "expiry": expiry,  # CFD cash indices use "-" (undated); spreadbet uses "DFB"
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

        r = self._request("POST", f"{DEMO_BASE}/positions/otc", "2", data=json.dumps(payload))
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Open position failed: HTTP {r.status_code} {r.text[:400]}")
        deal_ref = r.json().get("dealReference")

        confirm = self.deal_confirm(deal_ref)
        return deal_ref, confirm

    def deal_confirm(self, deal_reference: str) -> Dict[str, Any]:
        """Retrieve a structured confirmation for a prior dealReference.

        Args:
            deal_reference: The dealReference returned by a dealing request.

        Returns:
            Parsed JSON from GET /confirms/{dealReference}.

        Raises:
            requests.HTTPError: If the response is not successful.
        """
        r = self._request("GET", f"{DEMO_BASE}/confirms/{deal_reference}", "1")
        r.raise_for_status()
        return r.json()

    def list_positions(self) -> Dict[str, Any]:
        """List all open positions on the active account.

        Returns:
            Parsed JSON from GET /positions (VERSION 2).

        Raises:
            requests.HTTPError: If the response is not successful.
        """
        r = self._request("GET", f"{DEMO_BASE}/positions", "2")
        r.raise_for_status()
        return r.json()

    def close_position_market(self, deal_id: str, direction_open: str, size: float,
                              epic: str | None = None, expiry: str | None = None,
                              currency: str | None = None) -> str:
        """Close an open position using the most reliable available method.

        This performs three strategies in order:
          1) DELETE /positions/otc with a JSON body (official).
          2) POST /positions/otc with X-HTTP-Method-Override: DELETE (workaround).
          3) Netting-off fallback: send opposite MARKET order with forceOpen=False.

        Args:
            deal_id: The dealId of the open position to close.
            direction_open: The original open direction ('BUY' or 'SELL').
            size: Size to close.
            epic: Optional epic used for netting fallback.
            expiry: Optional expiry used for netting fallback.
            currency: Optional currency used for netting fallback.

        Returns:
            dealReference string from the successful close request (may be empty if
            the endpoint returned 2xx without a body).

        Raises:
            RuntimeError: If all close strategies fail.
        """
        """
        1) Try DELETE /positions/otc (official).
        2) If body is ignored (e.g., Demo 400 validation.null-not-allowed.request), retry using
           POST + X-HTTP-Method-Override: DELETE.
        3) If still failing, "net off": send opposite MARKET order with forceOpen=False.
        """
        opposite = "SELL" if (direction_open or "").upper() == "BUY" else "BUY"
        payload = {
            "dealId": deal_id,
            "direction": opposite,
            "size": float(size),
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
        }

        # --- 1) Official DELETE ---
        r = self._request("DELETE", f"{DEMO_BASE}/positions/otc", "1", json=payload)
        if r.status_code in (200, 201):
            try:
                return r.json().get("dealReference", "")
            except Exception:
                return ""

        # Parse errorCode if present
        err_code = None
        try:
            err_code = r.json().get("errorCode")
        except Exception:
            pass
        logging.warning("Close DELETE failed %s: %s", r.status_code, err_code or r.text[:200])

        # --- 2) Method-override POST (community workaround) ---
        if r.status_code == 400 and (err_code in ("validation.null-not-allowed.request", "invalid.input", None)):
            hdrs = self._headers("1")
            hdrs["X-HTTP-Method-Override"] = "DELETE"
            r2 = self.s.post(f"{DEMO_BASE}/positions/otc", headers=hdrs, json=payload, timeout=20)
            if r2.status_code in (200, 201):
                try:
                    return r2.json().get("dealReference", "")
                except Exception:
                    return ""
            try:
                logging.warning("Close POST override failed %s: %s", r2.status_code, r2.json().get("errorCode"))
            except Exception:
                logging.warning("Close POST override failed %s", r2.status_code)

        # --- 3) Net-off fallback (opposite order, forceOpen=False) ---
        if not epic:
            # As a safeguard, try to read the position again to get epic/expiry/currency
            try:
                pos = self._request("GET", f"{DEMO_BASE}/positions/{deal_id}", "2")
                if pos.status_code == 200:
                    pdata = pos.json().get("position", {}) or {}
                    epic = pdata.get("epic", epic)
                    expiry = pdata.get("expiry", expiry)
                    currency = pdata.get("currency", currency)
            except Exception:
                pass

        if epic and currency:
            rev = {
                "epic": epic,
                "expiry": expiry or "-",
                "direction": opposite,
                "size": float(size),
                "orderType": "MARKET",
                "timeInForce": "FILL_OR_KILL",
                "forceOpen": False,  # critical for netting off
                "guaranteedStop": False,
                "currencyCode": currency,
            }
            r3 = self._request("POST", f"{DEMO_BASE}/positions/otc", "2", json=rev)
            if r3.status_code in (200, 201):
                try:
                    return r3.json().get("dealReference", "")
                except Exception:
                    return ""
            try:
                logging.error("Net-off fallback failed %s: %s", r3.status_code, r3.json().get("errorCode"))
            except Exception:
                logging.error("Net-off fallback failed %s", r3.status_code)

        raise RuntimeError(f"Close position failed: HTTP {r.status_code} {err_code or r.text[:200]}")

# ----- sizing & selection -----

def _points_per_pip(one_pip_means: str) -> float:
    """Parse the instrument's 'onePipMeans' text and return points per pip.

    Args:
        one_pip_means: Free-text like '1 point' or '0.1 points'.

    Returns:
        Floating number of *points* represented by one pip. Defaults to 1.0 on
        any parse error or empty input.
    """
    if not one_pip_means:
        return 1.0
    try:
        return float(one_pip_means.strip().split()[0])
    except Exception:
        return 1.0

def choose_germany40_epic(ig: IGRest) -> Tuple[str, Dict[str, Any]]:
    """Select a Germany 40 market epic that matches the account's unit type.

    The function searches several common terms, filters to INDICES instruments
    with the expected unit ('CONTRACTS' for CFD, 'AMOUNT' for spread bet), prefers
    EUR-denominated and tradeable markets, and returns the candidate with the
    smallest pip value (typically the mini contract).

    Args:
        ig: An authenticated IGRest client.

    Returns:
        Tuple (epic, details_json) for the chosen market.

    Raises:
        RuntimeError: If no suitable Germany 40 instrument is found.
    """
    want_unit = "CONTRACTS" if (ig.account_type or "CFD").upper() == "CFD" else "AMOUNT"
    # Try a few common search terms
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
            # Prefer EUR & tradeable
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

    # Prefer the smallest pip value (often the mini contract)
    candidates.sort(key=lambda x: x[1])
    epic, _, status, details = candidates[0]
    logging.info("Chosen EPIC %s (status=%s, unit=%s)", epic, status, details["instrument"]["unit"])
    return epic, details

def compute_size_and_distances(details: Dict[str, Any], target_eur: float, max_margin_eur: float) -> Tuple[float, float, float, str]:
    """Compute deal size, TP/SL distances (points), and currency for ~target P&L.

    The algorithm:
      - Starts with minimum viable size, computes points to target ≈ target_eur.
      - Ensures distances respect min stop/limit rules.
      - Estimates margin and scales size down if it exceeds max_margin_eur.

    Args:
        details: Instrument details from market_details().
        target_eur: Desired take-profit value per trade in EUR (approximate).
        max_margin_eur: Maximum estimated margin budget to allow.

    Returns:
        Tuple (size, limit_distance_points, stop_distance_points, currency_code),
        all rounded reasonably for submission.

    Notes:
        Uses STOP_TO_LIMIT_MULTIPLIER for SL distance (> min stop).
    """
    instr = details["instrument"]
    rules = details.get("dealingRules", {})

    ppp = _points_per_pip(instr.get("onePipMeans", "1"))
    pip_value_eur = float(instr.get("valueOfOnePip"))
    currency = next(
        (c["code"] for c in instr.get("currencies", []) if c.get("isDefault")),
        (instr.get("currencies", [{}])[0].get("code", "EUR") if instr.get("currencies") else "EUR")
    )

    min_limit = float(rules.get("minNormalStopOrLimitDistance", {}).get("value", 0.1))
    min_stop  = float(rules.get("minNormalStopOrLimitDistance", {}).get("value", 0.1))
    min_size  = float(rules.get("minDealSize", {}).get("value", 0.1))

    # start with size=1, derive points for ≈€1
    size = max(1.0, min_size)
    pips_needed = max(1e-9, target_eur / (pip_value_eur * size))
    points_needed = pips_needed * ppp
    if points_needed < min_limit:
        pips_needed = max(1e-9, (min_limit / ppp))
        size = max(min_size, target_eur / (pip_value_eur * pips_needed))
    limit_distance = max(min_limit, points_needed)
    stop_distance  = max(min_stop,  limit_distance * STOP_TO_LIMIT_MULTIPLIER)

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
        stop_distance  = max(min_stop,  limit_distance * STOP_TO_LIMIT_MULTIPLIER)

    return round(size, 2), round(limit_distance, 2), round(stop_distance, 2), currency

# ----- micro strategy & lifecycle -----

def momentum_direction(ig: IGRest, epic: str) -> str:
    """Tiny momentum heuristic based on last two 1-minute candle closes.

    Args:
        ig: IGRest client.
        epic: Market epic to inspect.

    Returns:
        'BUY' if the most recent close is >= previous close, else 'SELL'.
        Falls back to 'BUY' on any error or insufficient data.
    """
    """Very small REST-only momentum: compare last two 1-min closes."""
    try:
        pr = ig.recent_prices(epic, "MINUTE", 3).get("prices", [])
        if len(pr) >= 2:
            def mid(px): return px.get("closePrice", {}).get("mid") or (
                (px["closePrice"].get("bid") + px["closePrice"].get("ask")) / 2.0
            )
            return "BUY" if mid(pr[-1]) >= mid(pr[-2]) else "SELL"
    except Exception:
        pass
    return "BUY"

def wait_until_closed_or_timeout(ig: IGRest, deal_id: str, max_wait_s: int) -> bool:
    """Poll open positions until a given dealId disappears or a timeout elapses.

    Args:
        ig: IGRest client.
        deal_id: Deal id to watch for closure.
        max_wait_s: Maximum seconds to wait before giving up.

    Returns:
        True if the position was closed within the timeout; False otherwise.
    """
    start = time.time()
    while not stop_event.is_set() and (time.time() - start) < max_wait_s:
        try:
            pos = ig.list_positions()
            if not any(p.get("position", {}).get("dealId") == deal_id for p in pos.get("positions", [])):
                return True
        except Exception:
            pass
        time.sleep(POLL_POSITIONS_SEC)
    return False

def close_all_positions(ig: IGRest, epic: str | None = None) -> None:
    """Attempt to close all (or all matching) open positions at market.

    Iterates through GET /positions and calls close_position_market for each,
    logging any failures but continuing through the list.

    Args:
        ig: IGRest client.
        epic: Optional epic to filter; if provided, only positions on this epic
            will be closed.
    """
    try:
        pos = ig.list_positions()
        for p in pos.get("positions", []):
            position = p.get("position", {}) or {}
            market   = p.get("market", {}) or {}
            deal_id   = position.get("dealId")
            direction = position.get("direction")
            size      = float(position.get("size", 0) or 0)
            inst_epic = position.get("epic") or market.get("epic")
            expiry    = position.get("expiry") or market.get("expiry") or "-"
            currency  = position.get("currency") or market.get("currency") or "EUR"
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


def main():
    """Run the demo scalper loop until target is reached or a stop signal arrives.

    Flow:
        - Login and select a Germany 40 market matching the account type.
        - Compute size/TP/SL to target ≈ €1 per trade within a margin budget.
        - Repeatedly place a MARKET order following a tiny momentum signal and
          wait for TP or timeout; on timeout, close manually.
        - Re-compute sizing periodically in case of rules/price changes.
        - On exit or signal, attempt to close any remaining positions and logout.
    """
    api_key   = os.environ.get("API_KEY")
    username  = os.environ.get("USERNAME")
    password  = os.environ.get("PASSWORD")
    account_id= os.environ.get("ACCOUNT_ID")

    if not all([api_key, username, password]):
        print("Set API_KEY, USERNAME, PASSWORD (and ACCOUNT_ID) as env vars.", file=sys.stderr)
        sys.exit(2)

    ig = IGRest(api_key, username, password, account_id)

    def handle_signal(sig, frame):
        """Signal handler to stop the run, close positions, logout, and exit.

        Args:
            sig: The received signal number.
            frame: Current stack frame (ignored).
        """
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

        realized_approx = 0.0
        trade_num = 0

        while (realized_approx + 1e-6) < DAILY_TARGET_EUR and not stop_event.is_set():
            trade_num += 1
            direction = momentum_direction(ig, epic)
            logging.info("Trade #%d: %s %s size=%.2f | TP=%.2f pts (≈€%.2f), SL=%.2f pts",
                         trade_num, direction, epic, size, tp_pts, PER_TRADE_TARGET_EUR, sl_pts)

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

            closed_by_tp = wait_until_closed_or_timeout(ig, deal_id, MAX_HOLD_SECONDS)
            if not closed_by_tp:
                logging.info("Timeout: closing manually at market…")
                try:
                    pos = ig.list_positions()
                    for p in pos.get("positions", []):
                        if p.get("position", {}).get("dealId") == deal_id:
                            ig.close_position_market(deal_id, p["position"]["direction"], float(p["position"]["size"]))
                            break
                except Exception as e:
                    logging.warning("Manual close failed: %s", e)

            if closed_by_tp:
                realized_approx += PER_TRADE_TARGET_EUR
                logging.info("TP hit. Session realized ≈ €%.2f / €%.2f target", realized_approx, DAILY_TARGET_EUR)
            else:
                logging.info("Closed manually; not counting this towards €1-per-trade goal.")

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
    from dotenv import load_dotenv

    load_dotenv()
    main()
