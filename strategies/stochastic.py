"""
stochastic.py — IG 'Stochastic oscillator' scalping:
%K/%D cross from oversold/overbought with 200-SMA trend filter.
"""

from indicators import extract_ohlc, sma, stoch_kd


def stochastic_direction(ig, epic: str, k_period: int, d_period: int,
                         k_lo: float, k_hi: float, ma_trend: int) -> str | None:
    need = max(ma_trend + k_period + d_period + 2, 230)
    bars = ig.recent_prices(epic, "MINUTE", need).get("prices", [])
    closes, highs, lows = extract_ohlc(bars)
    if len(closes) < ma_trend + k_period + d_period:
        return None
    ma200_now = sma(closes, ma_trend)
    ma200_prev = sma(closes[:-1], ma_trend)
    trend_up = ma200_now > ma200_prev
    trend_down = ma200_now < ma200_prev

    k_vals, d_vals = stoch_kd(closes, highs, lows, k_period, d_period)
    if len(k_vals) < 2 or len(d_vals) < 2:
        return None
    k_prev, k_now = k_vals[-2], k_vals[-1]
    d_prev, d_now = d_vals[-2], d_vals[-1]
    bull_cross = k_prev <= d_prev and k_now > d_now and k_prev < k_lo
    bear_cross = k_prev >= d_prev and k_now < d_now and k_prev > k_hi
    if bull_cross and trend_up:
        return "BUY"
    if bear_cross and trend_down:
        return "SELL"
    return None
