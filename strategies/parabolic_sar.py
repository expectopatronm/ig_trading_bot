"""
parabolic_sar.py — IG 'Parabolic SAR' scalping:
Enter when the close crosses the SAR (flip).
"""

from indicators import extract_ohlc, parabolic_sar_series

def psar_direction(ig, epic: str, af: float, af_max: float) -> str | None:
    need = 150
    bars = ig.recent_prices(epic, "MINUTE", need).get("prices", [])
    closes, highs, lows = extract_ohlc(bars)
    if len(highs) < 5:
        return None
    sar = parabolic_sar_series(highs, lows, af, af_max, closes)
    if len(sar) < 2:
        return None
    prev_above = closes[-2] > sar[-2]
    now_above  = closes[-1] > sar[-1]
    if (not prev_above) and now_above:
        return "BUY"
    if prev_above and (not now_above):
        return "SELL"
    return None
