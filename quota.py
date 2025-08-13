# quota.py
from __future__ import annotations
import threading, time, collections, re, logging
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

# Default heuristics (can be overridden via env-driven config in main)
DEFAULT_TRADE_PER_MIN = 35         # IG docs often cite ~40/min; keep a safety buffer
DEFAULT_DATA_PER_MIN  = 120        # data/snapshots/etc.
DEFAULT_HIST_POINTS_WEEK = 10000   # common demo allowance; varies

# Simple URL bucketing
_TRADE_PAT  = re.compile(r"/positions/otc|/workingorders", re.I)
_DATA_PAT   = re.compile(r"/prices/|/markets/|/positions\b|/clientsentiment", re.I)
_AUTH_PAT   = re.compile(r"/session\b|/session/refresh-token\b", re.I)

def bucket_for(method: str, url: str) -> str:
    if _TRADE_PAT.search(url) and method.upper() in ("POST", "PUT", "DELETE"):
        return "trade"
    if _AUTH_PAT.search(url):
        return "auth"
    if _DATA_PAT.search(url) or method.upper() == "GET":
        return "data"
    return "other"

@dataclass
class RateLimits:
    trade_per_min: int = DEFAULT_TRADE_PER_MIN
    data_per_min:  int = DEFAULT_DATA_PER_MIN
    hist_points_week: int = DEFAULT_HIST_POINTS_WEEK

@dataclass
class WindowCounter:
    """Rolling 60s window counter."""
    stamps: Deque[float] = field(default_factory=collections.deque)

    def add(self, now: float) -> None:
        self.stamps.append(now)
        self._trim(now)

    def count_last_60s(self, now: float) -> int:
        self._trim(now)
        return len(self.stamps)

    def _trim(self, now: float) -> None:
        cutoff = now - 60.0
        while self.stamps and self.stamps[0] < cutoff:
            self.stamps.popleft()

    def seconds_until_reset(self, now: float) -> int:
        self._trim(now)
        if not self.stamps:
            return 0
        return max(0, int((self.stamps[0] + 60.0) - now))

@dataclass
class WeeklyPoints:
    """Rolling 7-day datapoint usage (for historical candles)."""
    points: Deque[Tuple[float, int]] = field(default_factory=collections.deque)

    def add(self, now: float, n: int) -> None:
        if n <= 0:
            return
        self.points.append((now, n))
        self._trim(now)

    def used_last_7d(self, now: float) -> int:
        self._trim(now)
        return sum(n for _, n in self.points)

    def _trim(self, now: float) -> None:
        cutoff = now - 7 * 24 * 3600
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()

class QuotaTracker:
    """Tracks per-bucket request rates and historical datapoints; surfaces server rate headers if present."""
    def __init__(self, limits: RateLimits):
        self.lim = limits
        self.lock = threading.Lock()
        self.win: Dict[str, WindowCounter] = {
            "trade": WindowCounter(),
            "data":  WindowCounter(),
            "auth":  WindowCounter(),
            "other": WindowCounter(),
        }
        self.week = WeeklyPoints()
        self.server_headers: Dict[str, str] = {}  # last seen X-RateLimit-* values

    def record_call(self, method: str, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        b = bucket_for(method, url)
        now = time.time()
        with self.lock:
            self.win[b].add(now)
            if headers:
                for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                          "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
                    if k in headers:
                        self.server_headers[k] = headers[k]
        return b

    def record_hist_points(self, n_points: int) -> None:
        if n_points <= 0:
            return
        with self.lock:
            self.week.add(time.time(), n_points)

    def snapshot(self) -> Dict[str, Dict[str, int | str]]:
        now = time.time()
        with self.lock:
            td_used  = self.win["trade"].count_last_60s(now)
            dt_used  = self.win["data"].count_last_60s(now)
            au_used  = self.win["auth"].count_last_60s(now)
            ot_used  = self.win["other"].count_last_60s(now)
            td_rem   = max(0, self.lim.trade_per_min - td_used)
            dt_rem   = max(0, self.lim.data_per_min  - dt_used)
            td_reset = self.win["trade"].seconds_until_reset(now)
            dt_reset = self.win["data"].seconds_until_reset(now)
            week_used = self.week.used_last_7d(now)
            week_rem  = max(0, self.lim.hist_points_week - week_used)
            return {
                "trade": {"used": td_used, "limit": self.lim.trade_per_min, "remaining": td_rem, "reset_s": td_reset},
                "data":  {"used": dt_used, "limit": self.lim.data_per_min,  "remaining": dt_rem, "reset_s": dt_reset},
                "auth":  {"used": au_used},
                "other": {"used": ot_used},
                "hist":  {"used": week_used, "limit": self.lim.hist_points_week, "remaining": week_rem},
                "headers": self.server_headers.copy(),
            }

class QuotaReporter(threading.Thread):
    """Logs a one-liner quota snapshot every `interval_sec` seconds."""
    def __init__(self, tracker: QuotaTracker, interval_sec: float = 30.0, stop_evt: Optional[threading.Event] = None):
        super().__init__(daemon=True)
        self.t = tracker
        self.iv = max(5.0, float(interval_sec))
        self.stop_evt = stop_evt

    def run(self):
        while not (self.stop_evt and self.stop_evt.is_set()):
            snap = self.t.snapshot()
            h = snap["headers"]
            hdr = f" | hdr rem={h.get('X-RateLimit-Remaining') or h.get('x-ratelimit-remaining')}" if h else ""
            logging.info(
                "Quota | trade %d/%d (rem %d, %ss) | data %d/%d (rem %d, %ss) | hist %d/%d (rem %d)%s",
                snap["trade"]["used"], snap["trade"]["limit"], snap["trade"]["remaining"], snap["trade"]["reset_s"],
                snap["data"]["used"],  snap["data"]["limit"],  snap["data"]["remaining"],  snap["data"]["reset_s"],
                snap["hist"]["used"],  snap["hist"]["limit"],  snap["hist"]["remaining"],
                hdr
            )
            time.sleep(self.iv)
