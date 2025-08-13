"""
config.py — Centralized constants & tunables.

All values can be overridden via environment variables where it makes sense.
This keeps 'what rarely changes' in one place, away from main.py.
"""
import os
from typing import List, Tuple

# ===== Strategy targets & risk =====
PER_TRADE_TARGET_EUR: float = float(os.environ.get("PER_TRADE_TARGET_EUR", "1.0"))
DAILY_TARGET_EUR: float = float(os.environ.get("DAILY_TARGET_EUR", "10.0"))
STOP_TO_LIMIT_MULTIPLIER: float = float(os.environ.get("STOP_TO_LIMIT_MULTIPLIER", "3.0"))

# ===== Ledger / balance =====
# Starting balance used to initialise the ledger the very first time only.
START_BALANCE_EUR: float = float(os.environ.get("START_BALANCE_EUR", "500.0"))
LEDGER_DIR: str = os.environ.get("LEDGER_DIR", "ledger")
LEDGER_TRADES_CSV: str = os.environ.get("LEDGER_TRADES_CSV", os.path.join(LEDGER_DIR, "trades.csv"))
LEDGER_STATE_JSON: str = os.environ.get("LEDGER_STATE_JSON", os.path.join(LEDGER_DIR, "state.json"))

# ===== Entry filters / management =====
EMA_PERIOD: int = int(os.environ.get("EMA_PERIOD", "20"))
ATR_PERIOD: int = int(os.environ.get("ATR_PERIOD", "14"))
ATR_MIN_THRESHOLD: float = float(os.environ.get("ATR_MIN_THRESHOLD", "3.0"))
SPREAD_MAX_POINTS: float = float(os.environ.get("SPREAD_MAX_POINTS", "3.0"))
BREAKEVEN_TRIGGER_RATIO: float = float(os.environ.get("BREAKEVEN_TRIGGER_RATIO", "0.5"))
BREAKEVEN_OFFSET_POINTS: float = float(os.environ.get("BREAKEVEN_OFFSET_POINTS", "0.1"))
TRAIL_DIST_ATR_MULT: float = float(os.environ.get("TRAIL_DIST_ATR_MULT", "0.8"))
TRAIL_STEP_ATR_MULT: float = float(os.environ.get("TRAIL_STEP_ATR_MULT", "0.3"))
MIN_TRAIL_STEP_POINTS: float = float(os.environ.get("MIN_TRAIL_STEP_POINTS", "0.1"))

# ===== Session filter (Europe/Berlin) =====
SESSION_FILTER_ENABLED: bool = os.environ.get("SESSION_FILTER_ENABLED", "true").lower() == "true"
SESSION_SKIP_WEEKENDS: bool = os.environ.get("SESSION_SKIP_WEEKENDS", "true").lower() == "true"
SESSION_WINDOWS_LOCAL: List[Tuple[str, str]] = [
    tuple(x.strip() for x in w.split(","))
    for w in os.environ.get("SESSION_WINDOWS_LOCAL", "09:05,11:15;15:30,17:05").split(";")
]
SESSION_IDLE_SLEEP_SECONDS: float = float(os.environ.get("SESSION_IDLE_SLEEP_SECONDS", "30.0"))

# ===== Infra =====
POLL_POSITIONS_SEC: float = float(os.environ.get("POLL_POSITIONS_SEC", "5.0"))
RETRY_BACKOFF_SEC: float = float(os.environ.get("RETRY_BACKOFF_SEC", "1.0"))

# ===== Strategy selection & indicator params =====
# Options: "stochastic", "moving_average", "parabolic_sar", "rsi", "micro_momentum"
SCALP_STRATEGY: str = os.environ.get("SCALP_STRATEGY", "micro_momentum").strip().lower()

# Stochastic settings
STO_K_PERIOD: int = int(os.environ.get("STO_K_PERIOD", "14"))
STO_D_PERIOD: int = int(os.environ.get("STO_D_PERIOD", "3"))
STO_LO: float = float(os.environ.get("STO_LO", "20.0"))
STO_HI: float = float(os.environ.get("STO_HI", "80.0"))

# Moving averages strategy settings
MA_FAST: int = int(os.environ.get("MA_FAST", "5"))
MA_SLOW: int = int(os.environ.get("MA_SLOW", "20"))
MA_TREND: int = int(os.environ.get("MA_TREND", "200"))

# RSI settings
RSI_PERIOD: int = int(os.environ.get("RSI_PERIOD", "14"))
RSI_LO: float = float(os.environ.get("RSI_LO", "30.0"))
RSI_HI: float = float(os.environ.get("RSI_HI", "70.0"))

# Parabolic SAR settings (Wilder)
PSAR_AF: float = float(os.environ.get("PSAR_AF", "0.02"))
PSAR_AF_MAX: float = float(os.environ.get("PSAR_AF_MAX", "0.2"))

# ===== Quota / rate tracking (estimated; used by quota.py) =====
QUOTA_REPORT_EVERY_SEC: float = float(os.environ.get("QUOTA_REPORT_EVERY_SEC", "30"))
EST_TRADE_PER_MIN: int = int(os.environ.get("EST_TRADE_PER_MIN", "35"))       # ~40/min published; keep buffer
EST_DATA_PER_MIN: int = int(os.environ.get("EST_DATA_PER_MIN", "120"))
EST_HIST_POINTS_WEEK: int = int(os.environ.get("EST_HIST_POINTS_WEEK", "10000"))

# ===== Historical usage protection & price caching =====
PRICE_CACHE_ENABLED: bool = os.environ.get("PRICE_CACHE_ENABLED", "true").lower() == "true"
PRICE_CACHE_STALE_SEC: float = float(os.environ.get("PRICE_CACHE_STALE_SEC", "300"))
HIST_RESERVE_POINTS: int = int(os.environ.get("HIST_RESERVE_POINTS", "2000"))
