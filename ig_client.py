"""
ig_client.py — Minimal IG REST API client focused on market, pricing, and OTC dealing calls.
REST only (no streaming). Handles 401 refresh and common dealing ops.
Now augmented to feed the QuotaTracker with every request and to count historical datapoints.
"""

import time
import logging
import requests
from typing import Dict, Any, Optional, Tuple, List

from quota import QuotaTracker  # safe: quota has no dependency on ig_client
from config import PRICE_CACHE_ENABLED, PRICE_CACHE_STALE_SEC, HIST_RESERVE_POINTS

DEMO_BASE = "https://demo-api.ig.com/gateway/deal"


class IGRest:
    """IG REST wrapper with auth, market/prices, and OTC dealing helpers."""

    def __init__(self, api_key: str, username: str, password: str,
                 account_id: Optional[str] = None, tracker: Optional[QuotaTracker] = None):
        self.api_key = api_key
        self.username = username
        self.password = password
        self.account_id = account_id
        self.account_type = "CFD"
        self.cst = None
        self.xst = None
        self.s = requests.Session()
        self.tracker = tracker  # optional quota tracker
        # Price cache: throttle /prices to ~once per bar period
        self._price_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._price_last_fetch: Dict[Tuple[str, str], float] = {}

    # ----- low-level helpers -----
    # ----- resolution helpers -----
    @staticmethod
    def _res_seconds(resolution: str) -> int:
        r = (resolution or "MINUTE").upper()
        if r == "SECOND":
            return 1
        if r.startswith("MINUTE_"):
            try:
                return 60 * int(r.split("_")[1])
            except Exception:
                return 60
        if r == "MINUTE":
            return 60
        if r.startswith("HOUR_"):
            try:
                return 3600 * int(r.split("_")[1])
            except Exception:
                return 3600
        if r == "HOUR":
            return 3600
        if r == "DAY":
            return 86400
        return 60

    def _headers(self, version: Optional[str] = None) -> Dict[str, str]:
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
        """Send an HTTP request with IG headers and refresh tokens once on 401. Also record quota usage."""
        r = self.s.request(method, url, headers=self._headers(version), timeout=kwargs.pop("timeout", 20), **kwargs)
        try:
            if self.tracker:
                self.tracker.record_call(method, url, headers=r.headers)
        except Exception:
            pass

        if r.status_code == 401:
            try:
                logging.warning("401 on %s %s | body=%s", method, url, r.text[:300])
            except Exception as e:
                logging.warning("401 logging issue: %s", e)
            rr = self.s.post(f"{DEMO_BASE}/session/refresh-token", headers=self._headers("1"), timeout=10)
            if rr.status_code in (200, 201):
                self.cst = rr.headers.get("CST") or self.cst
                self.xst = rr.headers.get("X-SECURITY-TOKEN") or self.xst
                r = self.s.request(method, url, headers=self._headers(version), timeout=20, **kwargs)
                try:
                    if self.tracker:
                        self.tracker.record_call(method, url, headers=r.headers)
                except Exception:
                    pass
        return r

    # ----- session -----

    def login(self) -> None:
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

        if not self.account_id:
            try:
                sess = self.s.get(f"{DEMO_BASE}/session", headers=self._headers("1"), timeout=10).json()
                self.account_id = sess.get("currentAccountId")
            except Exception as e:
                logging.warning("%s", e)

        if self.account_id:
            r2 = self.s.put(f"{DEMO_BASE}/session",
                            headers=self._headers("1"),
                            json={"accountId": self.account_id, "defaultAccount": True},
                            timeout=10)
            if r2.status_code not in (200, 204):
                logging.warning("Preferred account set returned %s: %s", r2.status_code, r2.text[:200])

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
            logging.warning("%s", e)

    def logout(self) -> None:
        try:
            self.s.delete(f"{DEMO_BASE}/session", headers=self._headers("1"), timeout=10)
        except Exception as e:
            logging.warning("%s", e)

    # ----- markets & prices -----

    def search_markets(self, term: str) -> Dict[str, Any]:
        r = self._request("GET", f"{DEMO_BASE}/markets?searchTerm={requests.utils.quote(term)}", "1")
        if r.status_code >= 400:
            raise RuntimeError(f"search_markets failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def market_details(self, epic: str) -> Dict[str, Any]:
        r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "4")
        if r.status_code == 404:
            r = self._request("GET", f"{DEMO_BASE}/markets/{epic}", "3")
        if r.status_code >= 400:
            raise RuntimeError(f"market_details failed: {r.status_code} {r.text[:400]}")
        return r.json()

    def recent_prices(self, epic: str, resolution: str = "MINUTE", num_points: int = 3) -> Dict[str, Any]:
        """
        Fetch a small window of recent prices for a market (candles) and record datapoints.

        Quota-friendly behavior:
        - If PRICE_CACHE_ENABLED, and we fetched within the current bar period, serve from cache.
        - If weekly historical allowance is low (<= HIST_RESERVE_POINTS), prefer cached data (even if a bit stale)
          up to PRICE_CACHE_STALE_SEC, to avoid 403s.
        """
        key = (epic, (resolution or "MINUTE").upper())
        now = time.time()
        period = self._res_seconds(key[1])

        # Serve from cache if it's fresh enough and large enough
        cached = self._price_cache.get(key)
        last_fetch = self._price_last_fetch.get(key, 0.0)

        # Helper: do a network fetch
        def _fetch() -> Dict[str, Any]:
            r = self._request("GET", f"{DEMO_BASE}/prices/{epic}/{key[1]}/{num_points}", "2")
            if r.status_code >= 400:
                raise RuntimeError(f"recent_prices failed: {r.status_code} {r.text[:400]}")
            payload = r.json()
            bars = payload.get("prices", []) or []
            # Record datapoints only for network hits
            try:
                if self.tracker:
                    self.tracker.record_hist_points(len(bars))
            except Exception:
                pass
            # Update cache
            try:
                if PRICE_CACHE_ENABLED:
                    self._price_cache[key] = bars  # full replace is fine for 'recent' window
                    self._price_last_fetch[key] = time.time()
            except Exception:
                pass
            return payload

        # Check remaining weekly allowance (best-effort from tracker snapshot)
        hist_remaining = None
        try:
            if self.tracker:
                snap = self.tracker.snapshot()
                hist_remaining = int(snap.get("hist", {}).get("remaining"))
        except Exception:
            hist_remaining = None

        use_cache_due_to_quota = (
                PRICE_CACHE_ENABLED and cached and hist_remaining is not None and hist_remaining <= max(0,
                                                                                                        int(HIST_RESERVE_POINTS))
        )
        cache_fresh = cached is not None and (now - last_fetch) < max(1, int(0.95 * period))

        if PRICE_CACHE_ENABLED and cached and len(cached) >= max(1, num_points):
            if cache_fresh or use_cache_due_to_quota:
                # If we are using cache due to quota exhaustion, allow staleness up to PRICE_CACHE_STALE_SEC
                if use_cache_due_to_quota and (now - last_fetch) > PRICE_CACHE_STALE_SEC:
                    # Cache too stale, but still avoid 403: return what we have (may degrade signals)
                    return {"prices": cached[-num_points:]}
                # Fresh enough
                return {"prices": cached[-num_points:]}

        # Otherwise, fetch from network (first call or cache expired)
        return _fetch()

    # ----- dealing -----

    def open_market_position(
            self,
            epic: str,
            direction: str,
            size: float,
            currency: str,
            limit_distance_points: float,
            stop_distance_points: float | None,
    ) -> tuple[str, dict]:
        expiry = "-" if (self.account_type or "CFD").upper() == "CFD" else "DFB"
        payload = {
            "epic": epic,
            "expiry": expiry,
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
        r = self._request("GET", f"{DEMO_BASE}/confirms/{deal_reference}", "1")
        r.raise_for_status()
        return r.json()

    def list_positions(self) -> Dict[str, Any]:
        r = self._request("GET", f"{DEMO_BASE}/positions", "2")
        r.raise_for_status()
        return r.json()

    def close_position_market(self, deal_id: str, direction_open: str, size: float,
                              epic: str | None = None, expiry: str | None = None,
                              currency: str | None = None) -> str:
        opposite = "SELL" if (direction_open or "").upper() == "BUY" else "BUY"

        # Prefer net-off if possible
        if epic and currency:
            expiry = expiry or ("-" if (self.account_type or "CFD").upper() == "CFD" else "DFB")
            rev = {
                "epic": epic,
                "expiry": expiry,
                "direction": opposite,
                "size": float(size),
                "orderType": "MARKET",
                "timeInForce": "FILL_OR_KILL",
                "forceOpen": False,
                "currencyCode": currency
            }
            r = self._request("POST", f"{DEMO_BASE}/positions/otc", "2", json=rev)
            if r.status_code in (200, 201):
                try:
                    return r.json().get("dealReference", "")
                except Exception:
                    return ""
            try:
                logging.warning("%s", r.json().get("errorCode"))
            except Exception as e:
                logging.warning("%s", e)

        # Official DELETE
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

        # Method-override POST as last resort
        hdrs = self._headers("1")
        hdrs["X-HTTP-Method-Override"] = "DELETE"
        r2 = self.s.post(f"{DEMO_BASE}/positions/otc", headers=hdrs, json=payload, timeout=20)
        if r2.status_code in (200, 201):
            try:
                return r2.json().get("dealReference", "")
            except Exception:
                return ""

        err = None
        try:
            err = r.json().get("errorCode")
        except Exception:
            pass
        raise RuntimeError(f"Close position failed: HTTP {r.status_code} {err or r.text[:200]}")
