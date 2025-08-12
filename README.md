## IG REST DAX Scalper (Demo) — Summary

This script implements a **simple automated scalping strategy** for trading the *Germany 40 (DAX)* index using IG's REST API in a **demo environment**. It is designed for small, repeatable trades targeting about €1 profit per trade until a daily target of ~€10 is reached.

### Key Features
- **REST-only API usage** — no streaming.
- **CFD vs Spread Bet aware** — automatically selects correct expiry ("-" for CFD, "DFB" for spread bet).
- **Automatic token refresh** on HTTP 401 via `/session/refresh-token`.
- **Safe shutdown** — intercepts SIGINT/SIGTERM, closes open positions, and logs out cleanly.
- **Resilient dealing functions** with fallbacks for closing trades.

### Strategy Logic
1. **Login and Setup**
   - Authenticates with IG API.
   - Selects a *Germany 40* market that matches the account type (CFD → CONTRACTS, Spread Bet → AMOUNT).
   - Computes position size, take-profit (TP), and stop-loss (SL) distances to target ≈€1/trade within a margin budget (~€500).

2. **Trade Loop**
   - Uses a **tiny momentum heuristic**: compares the last two 1-minute candle closes to decide BUY or SELL.
   - Opens a market order with TP and SL attached.
   - Waits until TP is hit or a 5-minute timeout occurs, then closes manually if needed.
   - Adjusts trade size and distances dynamically if market rules or prices change.
   - Continues until ~€10 total profit is reached or a stop signal is received.

3. **Shutdown**
   - Closes any remaining open positions.
   - Logs out from the IG session.

### Technical Highlights
- Modular IGRest client handles:
  - Session management.
  - Market search and details retrieval.
  - Price history requests.
  - Opening and closing OTC positions with fallbacks.
- Sizing logic respects:
  - Instrument pip values and point sizes.
  - Minimum stop/limit distances.
  - Margin requirements.
- **Configurable constants**:
  - `PER_TRADE_TARGET_EUR`
  - `DAILY_TARGET_EUR`
  - `MAX_HOLD_SECONDS`
  - `STOP_TO_LIMIT_MULTIPLIER`
  - Polling and retry delays.

---
**Disclaimer:** This is a **demo-only example** for educational purposes. It is not production-ready and not intended for live trading without significant modifications, testing, and risk controls.

