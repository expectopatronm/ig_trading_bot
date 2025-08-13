"""
micro_momentum.py — Your original 'last two 1-min closes' momentum heuristic.
"""

import logging


def momentum_direction(ig, epic: str) -> str:
    try:
        pr = ig.recent_prices(epic, "MINUTE", 3).get("prices", [])
        if len(pr) >= 2:
            def mid(px):
                cp = px.get("closePrice", {})
                m = cp.get("mid")
                if m is None:
                    b = cp.get("bid");
                    a = cp.get("ask")
                    if b is not None and a is not None:
                        m = (b + a) / 2.0
                return m

            return "BUY" if float(mid(pr[-1])) >= float(mid(pr[-2])) else "SELL"
    except Exception as e:
        logging.error("%s", e)
    return "BUY"
