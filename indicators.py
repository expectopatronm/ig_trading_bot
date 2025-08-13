"""
indicators.py — Technical indicators & OHLC utilities.
Independent of strategy logic so they rarely change.
"""

import math
from typing import List, Dict, Any, Optional, Tuple


def ema(values: List[float], period: int) -> float:
    if not values:
        return float("nan")
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / float(period)


def _bar_mid(x: Dict[str, Any], key: str) -> Optional[float]:
    d = x.get(key, {})
    mid = d.get("mid")
    if mid is None:
        bid = d.get("bid")
        ask = d.get("ask")
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
    return float(mid) if mid is not None else None


def extract_ohlc(bars: List[Dict[str, Any]]) -> Tuple[List[float], List[float], List[float]]:
    closes, highs, lows = [], [], []
    for b in bars:
        c = _bar_mid(b, "closePrice")
        h = _bar_mid(b, "highPrice")
        l = _bar_mid(b, "lowPrice")
        if c is None or h is None or l is None:
            continue
        closes.append(c); highs.append(h); lows.append(l)
    return closes, highs, lows


def ema_of_closes(bars: List[Dict[str, Any]], period: int) -> float:
    closes, _, _ = extract_ohlc(bars)
    if len(closes) == 0:
        return float("nan")
    return ema(closes, period)


def compute_atr_points(bars: List[Dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        return float("nan")
    trs: List[float] = []
    prev_close = _bar_mid(bars[0], "closePrice")
    for b in bars[1:]:
        high = _bar_mid(b, "highPrice")
        low = _bar_mid(b, "lowPrice")
        close = _bar_mid(b, "closePrice")
        if high is None or low is None or prev_close is None:
            prev_close = close
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if len(trs) < period:
        return float("nan")
    return sum(trs[-period:]) / float(period)


def latest_mid_and_spread(bars: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    if not bars:
        return None, None
    cp = bars[-1].get("closePrice", {})
    bid, ask, mid = cp.get("bid"), cp.get("ask"), cp.get("mid")
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    spread = None
    if bid is not None and ask is not None:
        spread = ask - bid
    return (float(mid) if mid is not None else None, float(spread) if spread is not None else None)


def rsi_series(closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis: List[float] = []
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = float("inf") if avg_loss == 0 else (avg_gain / avg_loss)
        rsis.append(100.0 - (100.0 / (1.0 + rs)))
    return rsis


def stoch_kd(closes: List[float], highs: List[float], lows: List[float],
             k_period: int = 14, d_period: int = 3) -> Tuple[List[float], List[float]]:
    if len(closes) < k_period:
        return [], []
    k_vals = []
    for i in range(k_period - 1, len(closes)):
        window_h = max(highs[i - k_period + 1:i + 1])
        window_l = min(lows[i - k_period + 1:i + 1])
        denom = (window_h - window_l)
        k = 0.0 if denom == 0 else 100.0 * (closes[i] - window_l) / denom
        k_vals.append(k)
    d_vals = []
    for i in range(d_period - 1, len(k_vals)):
        d_vals.append(sum(k_vals[i - d_period + 1:i + 1]) / d_period)
    k_vals = k_vals[-len(d_vals):] if d_vals else []
    return k_vals, d_vals


def parabolic_sar_series(highs: List[float], lows: List[float],
                         af: float = 0.02, af_max: float = 0.2,
                         closes: list[float] | None = None) -> List[float]:
    n = len(highs)
    if n < 5:
        return []
    up = True
    if closes and len(closes) >= 2:
        up = closes[1] >= closes[0]
    ep = highs[0] if up else lows[0]
    sar = [lows[0] if up else highs[0]]
    a = af
    for i in range(1, n):
        s_prev = sar[-1]
        s = s_prev + a * (ep - s_prev)
        if up:
            s = min(s, lows[i - 1], lows[i])
            if highs[i] > ep:
                ep = highs[i]; a = min(af_max, a + af)
            if lows[i] < s:
                up = False
                s = max(highs[i - 1], highs[i])
                ep = lows[i]
                a = af
        else:
            s = max(s, highs[i - 1], highs[i])
            if lows[i] < ep:
                ep = lows[i]; a = min(af_max, a + af)
            if highs[i] > s:
                up = True
                s = min(lows[i - 1], lows[i])
                ep = highs[i]
                a = af
        sar.append(s)
    return sar
