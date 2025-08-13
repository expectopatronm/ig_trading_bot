"""
moving_average.py — IG 'Moving Averages' scalping:
5/20 SMA cross with 200-SMA trend filter.
"""

import math
from indicators import extract_ohlc, sma


def ma_direction(ig, epic: str, ma_fast: int, ma_slow: int, ma_trend: int) -> str | None:
    need = max(ma_trend + ma_slow + 2, 230)
    bars = ig.recent_prices(epic, "MINUTE", need).get("prices", [])
    closes, _, _ = extract_ohlc(bars)
    if len(closes) < ma_trend + ma_slow + 1:
        return None
    ma200_now = sma(closes, ma_trend)
    ma200_prev = sma(closes[:-1], ma_trend)
    fast_now = sma(closes, ma_fast)
    fast_prev = sma(closes[:-1], ma_fast)
    slow_now = sma(closes, ma_slow)
    slow_prev = sma(closes[:-1], ma_slow)
    if any(map(lambda x: math.isnan(x), [ma200_now, ma200_prev, fast_now, fast_prev, slow_now, slow_prev])):
        return None
    bull_cross = fast_prev <= slow_prev and fast_now > slow_now
    bear_cross = fast_prev >= slow_prev and fast_now < slow_now
    trend_up = ma200_now > ma200_prev
    trend_down = ma200_now < ma200_prev
    if bull_cross and trend_up:
        return "BUY"
    if bear_cross and trend_down:
        return "SELL"
    return None
