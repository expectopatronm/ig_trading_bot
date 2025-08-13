"""
strategies — Entry signal implementations and a simple router.
This is the bit you'll tweak most often.
"""

from typing import Optional
from ig_client import IGRest
from config import (
    SCALP_STRATEGY, MA_FAST, MA_SLOW, MA_TREND,
    STO_K_PERIOD, STO_D_PERIOD, STO_LO, STO_HI,
    RSI_PERIOD, RSI_LO, RSI_HI,
    PSAR_AF, PSAR_AF_MAX
)
from indicators import sma, rsi_series, stoch_kd, parabolic_sar_series, extract_ohlc

from .micro_momentum import momentum_direction
from .moving_average import ma_direction
from .stochastic import stochastic_direction
from .parabolic_sar import psar_direction
from .rsi import rsi_direction


def choose_direction(ig: IGRest, epic: str, strategy: str | None = None) -> Optional[str]:
    """
    Return 'BUY' / 'SELL' / None using the named strategy.
    """
    s = (strategy or SCALP_STRATEGY).lower()
    if s == "micro_momentum":
        return momentum_direction(ig, epic)
    if s == "moving_average":
        return ma_direction(ig, epic, MA_FAST, MA_SLOW, MA_TREND)
    if s == "stochastic":
        return stochastic_direction(ig, epic, STO_K_PERIOD, STO_D_PERIOD, STO_LO, STO_HI, MA_TREND)
    if s == "parabolic_sar":
        return psar_direction(ig, epic, PSAR_AF, PSAR_AF_MAX)
    if s == "rsi":
        return rsi_direction(ig, epic, RSI_PERIOD, RSI_LO, RSI_HI, MA_TREND)
    return None
