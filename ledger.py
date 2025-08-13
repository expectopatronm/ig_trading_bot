"""
ledger.py — Simple local ledger for trades and rolling balance.

- Persists CSV of trades at ./ledger/trades.csv
- Persists state (current balance, today's anchor) at ./ledger/state.json
- Carries balance across days automatically. On a new day, today's start is set to current balance.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional

DEFAULT_DIR = os.environ.get("LEDGER_DIR", "ledger")
TRADES_CSV = os.environ.get("LEDGER_TRADES_CSV", os.path.join(DEFAULT_DIR, "trades.csv"))
STATE_JSON = os.environ.get("LEDGER_STATE_JSON", os.path.join(DEFAULT_DIR, "state.json"))


@dataclass
class Ledger:
    start_balance_default: float = 500.0
    _dir: str = field(default_factory=lambda: DEFAULT_DIR)
    _trades_csv: str = field(default_factory=lambda: TRADES_CSV)
    _state_json: str = field(default_factory=lambda: STATE_JSON)

    balance: float = field(init=False, default=0.0)
    day_start_balance: float = field(init=False, default=0.0)
    day: str = field(init=False, default="")

    def __post_init__(self) -> None:
        os.makedirs(self._dir, exist_ok=True)
        self._load_or_init_state()

    # --- state management -----------------------------------------------------
    def _load_or_init_state(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if os.path.exists(self._state_json):
            try:
                with open(self._state_json, "r", encoding="utf-8") as f:
                    st = json.load(f)
                self.balance = float(st.get("balance", self.start_balance_default))
                self.day = str(st.get("day") or today)
                self.day_start_balance = float(st.get("day_start_balance", self.balance))
            except Exception:
                # Corrupt state — reset
                self.balance = float(self.start_balance_default)
                self.day = today
                self.day_start_balance = self.balance
        else:
            self.balance = float(self.start_balance_default)
            self.day = today
            self.day_start_balance = self.balance
            self._flush_state()

        # New day? Reset the anchor
        if self.day != today:
            self.day = today
            self.day_start_balance = self.balance
            self._flush_state()

        # Ensure CSV has header
        if not os.path.exists(self._trades_csv):
            with open(self._trades_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp","epic","direction","size","currency",
                    "entry_level","exit_level","move_points","tp_points","sl_points",
                    "pnl_eur","balance_after","notes"
                ])

    def _flush_state(self) -> None:
        st = {"balance": self.balance, "day": self.day, "day_start_balance": self.day_start_balance}
        with open(self._state_json, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)

    # --- public API -----------------------------------------------------------
    def record_trade(self, trade: Dict[str, Any]) -> None:
        """Append a trade row and update balances."""
        pnl = float(trade.get("pnl_eur") or 0.0)
        self.balance = float(self.balance) + pnl

        # Ensure timestamps & defaults
        ts = trade.get("timestamp") or datetime.now(timezone.utc).isoformat()
        row = [
            ts,
            trade.get("epic"),
            trade.get("direction"),
            trade.get("size"),
            trade.get("currency") or "EUR",
            trade.get("entry_level"),
            trade.get("exit_level"),
            trade.get("move_points"),
            trade.get("tp_points"),
            trade.get("sl_points"),
            pnl,
            self.balance,
            trade.get("notes") or "",
        ]
        with open(self._trades_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        self._flush_state()

    def day_net(self) -> float:
        """Net P&L for the current day (balance - day_start_balance)."""
        return float(self.balance) - float(self.day_start_balance)

    # expose for logging use
    def get_paths(self) -> Dict[str, str]:
        return {"dir": self._dir, "trades_csv": self._trades_csv, "state_json": self._state_json}
