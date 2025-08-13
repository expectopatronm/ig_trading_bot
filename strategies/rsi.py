"""
rsi.py — IG 'RSI' scalping:
Take a rebound up from 30 (long) or a roll-off down from 70 (short), with 200-SMA trend filter.
"""

from indicators import extract_ohlc, rsi_series, sma


def rsi_direction(ig, epic: str, period: int, rsi_lo: float, rsi_hi: float, ma_trend: int) -> str | None:
    need = max(ma_trend + period + 2, 230)
    bars = ig.recent_prices(epic, "MINUTE", need).get("prices", [])
    closes, _, _ = extract_ohlc(bars)
    if len(closes) < ma_trend + period + 1:
        return None
    ma200_now = sma(closes, ma_trend)
    ma200_prev = sma(closes[:-1], ma_trend)
    trend_up = ma200_now > ma200_prev
    trend_down = ma200_now < ma200_prev

    rsis = rsi_series(closes, period)
    if len(rsis) < 2:
        return None
    r_prev, r_now = rsis[-2], rsis[-1]
    bull_rebound = (r_prev <= rsi_lo) and (r_now > rsi_lo)
    bear_rolloff = (r_prev >= rsi_hi) and (r_now < rsi_hi)
    if bull_rebound and trend_up:
        return "BUY"
    if bear_rolloff and trend_down:
        return "SELL"
    return None
