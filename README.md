# IG REST DAX Scalper

> **What this is:** a REST‑only Python bot that scalps the **Germany 40 (DAX)** on **IG’s DEMO**.  
> **Goal:** aim for ~**€1** per trade and repeat until **~€10/day**.  
> **New in v2:** no more hard timeouts — exits are driven by **price/volatility**: breakeven move, **trailing stop**, **signal invalidation**, and **ATR/spread gates**.  
> **Safety:** closes positions and logs out cleanly on exit/`Ctrl+C`.  
> **Disclaimer:** This is **not financial advice**. CFDs/spread bets are leveraged and can lose money rapidly. Use **demo** first.

---
## Strategy Reference

This script is a REST-only DAX (Germany 40) micro-scalper for IG’s demo API that hunts for ~€1 per trade and loops until about €10 daily, while tightly gating entries and managing risk. It auto-logs in, picks a Germany 40 epic matching the active account type (CFD vs spread-bet), then sizes each order from instrument metadata so the take-profit distance in points maps to ~€1 given pip value and minimum IG limits, with a stop set at 3× the TP distance (and overall margin kept modest). It trades only inside configurable Europe/Berlin session windows (skip weekends), and only if volatility and costs are acceptable: ATR(14, 1-min) must exceed a threshold and the current spread must be below a cap. Direction is a tiny momentum heuristic (BUY if the last 1-min close ≥ the prior, else SELL). After entry, a manager loop watches 1-min data: once price moves ≥50% of TP, it moves the stop to a tiny positive breakeven and enables a trailing stop whose distance and step scale with ATR (clamped by IG minimums). It will also exit early if the signal invalidates (price crosses the 20-EMA against the position) or if conditions deteriorate (ATR falls below the floor or spread spikes), and it continually re-derives sizing from fresh market details between trades. The program refreshes tokens on 401s, polls positions, and shuts down gracefully on signals by closing any open trades before logging out.

--- 

## Quick start

### Requirements
- Python 3.10+
- `pip install requests python-dotenv` (dotenv is optional, for `.env` loading)

### Configure environment variables (don’t hard‑code secrets)
```bash
# macOS / Linux
export API_KEY="your_demo_api_key"
export USERNAME="your_demo_username"
export PASSWORD="your_demo_password"
export ACCOUNT_ID="your_demo_account_id"   # optional if you have only one

# Windows (PowerShell)
setx API_KEY "your_demo_api_key"
setx USERNAME "your_demo_username"
setx PASSWORD "your_demo_password"
setx ACCOUNT_ID "your_demo_account_id"
```

> Or create a local `.env` file with the same keys; the script calls `load_dotenv(".env")` if available.

### Run
```bash
python main.py
```

You should see logs like:
```
INFO | Active account type: CFD
INFO | Chosen EPIC IX.D.DAX.IFMM.IP (status=TRADEABLE, unit=CONTRACTS)
INFO | Initial sizing: size=0.50, TP=5.00 pts, SL=15.00 pts, currency=EUR
INFO | Trade #1: BUY ...  Trailing activated: dist=0.80, step=0.30, stopLevel≈...
INFO | Favourable exit. Session realized ≈ €1.00 / €10.00 target
...
INFO | Target reached or stop requested. Tidying up…
INFO | Logged out. Bye.
```

---

## Key features

- **REST‑only** API usage — no streaming.
- **CFD vs Spread Bet aware** — selects correct **expiry** (`"-"` for CFD cash index, `"DFB"` for spread bet) and **instrument.unit** (CONTRACTS vs AMOUNT).
- **Automatic token refresh** on HTTP **401** via `POST /session/refresh-token`.
- **Resilient closing**: prefers **net‑off** (opposite MARKET with `forceOpen: false`), with DELETE/override fallbacks.
- **Graceful shutdown**: traps SIGINT/SIGTERM, closes positions, logs out.
- **Price‑based trade management** (v2):
  - **Breakeven move** then enable **trailing stop** (ATR‑scaled distance & increment).
  - **Signal invalidation** via **EMA(20)** on 1‑minute bars.
  - **Volatility/spread gates** both for **entry** and **early exit**.

---

## Strategy logic (v2)

1. **Login & Setup**
   - `POST /session` → store **CST** and **X‑SECURITY‑TOKEN**.
   - Set preferred account with `PUT /session` (prevents “preferred account not set” 401 variants).
   - Resolve **account type** via `GET /accounts`.
   - Find a **Germany 40** market via `GET /markets?searchTerm=...` then fetch `/markets/{epic}` and filter by **instrument.unit** to match account type:  
     **CFD → CONTRACTS & expiry "-"**, **Spread Bet → AMOUNT & expiry "DFB"**.

2. **Sizing**
   - From `/markets/{epic}` read **`valueOfOnePip`**, **`onePipMeans`**, **`dealingRules.minNormalStopOrLimitDistance`**, **`minDealSize`**, **`contractSize`**, **`marginDepositBands`**.
   - Compute size and TP distance targeting **≈€1** while meeting **min distance**/**min size**.  
   - Crude margin check scales size to stay ≈ **€500** budget by default.

3. **Entry gating**
   - Pull 1‑min bars via `GET /prices/{epic}/MINUTE/N`.  
   - Require **ATR(14) ≥ `ATR_MIN_THRESHOLD`** and **spread ≤ `SPREAD_MAX_POINTS`**.  
   - Simple **momentum bias**: last close ≥ previous → BUY else SELL.

4. **Open**
   - `POST /positions/otc` MARKET with attached `limitDistance` (TP) and `stopDistance` (SL).  
   - Confirm via `GET /confirms/{dealReference}`.

5. **Manage (no hard timeout)**
   - Track **entry price** and poll 1‑min bars:
     - **Breakeven + trailing stop:** once price moves ≥ **`BREAKEVEN_TRIGGER_RATIO` × TP distance**, set `trailingStop=True` via `PUT /positions/otc/{dealId}` with:
       - `trailingStopDistance ≈ ATR × TRAIL_DIST_ATR_MULT` (clamped ≥ IG min stop),
       - `trailingStopIncrement ≈ ATR × TRAIL_STEP_ATR_MULT` (≥ `MIN_TRAIL_STEP_POINTS`),
       - `stopLevel ≈ entry ± BREAKEVEN_OFFSET_POINTS`.
     - **Signal invalidation exit:** close if (long and close < EMA20) or (short and close > EMA20).
     - **Volatility/spread exit:** close if ATR drops below the entry gate or spread spikes above the cap.
   - If TP/SL/trailing is hit, the position disappears from `GET /positions`.

6. **Repeat until daily target**
   - Count **≈€1** for favourable exits (reached half‑TP and trailing engaged or TP close).  
   - Stop once **`DAILY_TARGET_EUR`** reached or on user signal.

7. **Shutdown**
   - Close any remaining open positions (prefer **net‑off**) and log out.

---

## Configuration knobs (edit constants at top of `main.py`)

- **Profit goals**  
  - `PER_TRADE_TARGET_EUR` (default **1.0**)  
  - `DAILY_TARGET_EUR` (default **10.0**)

- **Stops/limits**  
  - `STOP_TO_LIMIT_MULTIPLIER` (default **3.0**; SL distance as multiple of TP)

- **Filters & management**  
  - `EMA_PERIOD` (default **20**)  
  - `ATR_PERIOD` (default **14**)  
  - `ATR_MIN_THRESHOLD` (default **3.0** points)  
  - `SPREAD_MAX_POINTS` (default **3.0** points)  
  - `BREAKEVEN_TRIGGER_RATIO` (default **0.5**)  
  - `BREAKEVEN_OFFSET_POINTS` (default **0.1**)  
  - `TRAIL_DIST_ATR_MULT` (default **0.8**)  
  - `TRAIL_STEP_ATR_MULT` (default **0.3**)  
  - `MIN_TRAIL_STEP_POINTS` (default **0.1**)

- **Infra**  
  - `POLL_POSITIONS_SEC`, `RETRY_BACKOFF_SEC`

> **Removed in v2:** `MAX_HOLD_SECONDS` — there is **no time‑based close** anymore.

---

## Endpoints used (official IG docs)

> (URLs are indicative; refer to IG Labs for the latest versions.)

- **Login / session**  
  - `POST /session` — obtain **CST** and **X‑SECURITY‑TOKEN**  
  - `POST /session/refresh-token` — renew tokens on 401  
  - `PUT /session` — set preferred/default account  
  - `GET /accounts` — resolve account type

- **Markets / prices**  
  - `GET /markets?searchTerm=...` — search for DAX markets  
  - `GET /markets/{epic}` — instrument rules, pip values, min distances  
  - `GET /prices/{epic}/{resolution}/{numPoints}` — 1‑min candles

- **Dealing**  
  - `POST /positions/otc` — open MARKET (attach TP/SL)  
  - `GET /confirms/{dealReference}` — confirm deal status / id  
  - `PUT /positions/otc/{dealId}` — **update** position (breakeven/trailing stop)  
  - `GET /positions` — poll open positions  
  - `DELETE /positions/otc` — close at market (official)  
  - **Net‑off fallback:** `POST /positions/otc` with `forceOpen:false` (opposite MARKET)

Official docs hub: https://labs.ig.com/  
REST reference index: https://labs.ig.com/rest-trading-api-reference.html  
Markets (EPIC details): https://labs.ig.com/reference/markets-epic.html  
Streaming guide (token headers explained): https://labs.ig.com/streaming-api-guide.html

---

## Troubleshooting

- **412 `error.switch.accountId-must-be-different`**  
  You tried to set the preferred account to the one already set. Harmless; continue.

- **401 Unauthorized**  
  Tokens missing/expired. The bot auto‑refreshes via `/session/refresh-token` and retries once.

- **`REJECT_SPREADBET_ORDER_ON_CFD_ACCOUNT`**  
  Product mismatch. Ensure the chosen EPIC’s `instrument.unit` matches account type and expiry is correct (CFD `"-"`, Spread Bet `"DFB"`).

- **`400 validation.null-not-allowed.request` when closing**  
  Some stacks drop DELETE bodies. The bot prefers **net‑off**; if needed it attempts DELETE with JSON body and a method‑override POST fallback.

- **Min distances / sizes**  
  Read `dealingRules` in `/markets/{epic}`; the bot already adjusts but will log any rejections.

- **“Market not tradeable”**  
  See `snapshot.marketStatus` in `/markets/{epic}`; avoid closed/auction states.

---

## Security & safety

- **Rotate and never hard‑code secrets.** Keep them in env vars or a secret manager.  
- **Demo first.** Leverage magnifies losses as well as gains.  
- **Respect rate limits** (IG will return `error.public-api.exceeded-*` when you overrun allowances).

---

## Changelog

- **v2**
  - Removed time‑based close; added **breakeven + trailing stop**, **EMA20 invalidation**, **ATR/spread gates**.
  - Implemented `PUT /positions/otc/{dealId}` to manage trailing stops.
  - Prefer **net‑off** for closing (with DELETE/override fallbacks).
  - Cleaned request bodies (use `json=...`) and improved logs.
- **v1**
  - Basic momentum, fixed TP/SL, optional timeout close, session refresh, CFD/SpreadBet awareness.

---

*Run this on **DEMO** until you thoroughly test and tune it for your risk profile.*
