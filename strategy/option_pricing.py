"""
strategy/option_pricing.py — Black-Scholes option pricer + greeks.

Used as a placeholder when real OPRA bid/ask history is unavailable.
Swap out get_option_price() with a real data lookup once you have OPRA data.

All inputs use standard conventions:
    S     : underlying spot price
    K     : strike price
    T     : time to expiry in years  (trading days / 252)
    r     : annualised risk-free rate (e.g. 0.05 = 5%)
    sigma : annualised implied volatility (e.g. 0.18 = 18%)
"""

import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Normal distribution helpers (no scipy dependency)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Cumulative standard normal distribution."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class OptionPrice:
    """
    Result from the Black-Scholes pricer.

    price        : theoretical fair value (use as mid-price estimate)
    bid          : estimated bid  = price - half_spread
    ask          : estimated ask  = price + half_spread
    delta        : ∂V/∂S   — directional exposure (0–1 for calls)
    gamma        : ∂²V/∂S² — rate of delta change
    theta        : ∂V/∂t   — time decay per calendar day (negative)
    vega         : ∂V/∂σ   — sensitivity to 1% IV move
    iv           : implied volatility used (same as input sigma)
    intrinsic    : max(S-K, 0) for calls, max(K-S, 0) for puts
    time_value   : price - intrinsic
    """
    price:      float
    bid:        float
    ask:        float
    delta:      float
    gamma:      float
    theta:      float
    vega:       float
    iv:         float
    intrinsic:  float
    time_value: float
    option_type: str
    strike:     float
    underlying: float


# ---------------------------------------------------------------------------
# Pricer
# ---------------------------------------------------------------------------

def price_option(
    S: float,
    K: float,
    T: float,
    sigma: float,
    option_type: str = "CALL",
    r: float = 0.05,
    spread_pct: float = 0.05,       # 5% of mid as half-spread estimate
) -> OptionPrice:
    """
    Compute Black-Scholes price and greeks for a European option.

    Args:
        S           : underlying spot price
        K           : strike price
        T           : time to expiry in years  (e.g. 5/252 for 5 trading days)
        sigma       : implied volatility (annualised, e.g. 0.18)
        option_type : "CALL" or "PUT"
        r           : risk-free rate (annualised)
        spread_pct  : fraction of mid used to estimate bid/ask half-spread.
                      0.05 means bid = mid * 0.95, ask = mid * 1.05.
                      Replace with real bid/ask when OPRA data is available.

    Returns:
        OptionPrice dataclass with price, greeks, and estimated bid/ask.
    """
    option_type = option_type.upper()

    # Expired option — return intrinsic value only
    if T <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "CALL" else max(K - S, 0.0)
        return OptionPrice(
            price=intrinsic, bid=intrinsic, ask=intrinsic,
            delta=1.0 if (option_type == "CALL" and S > K) else 0.0,
            gamma=0.0, theta=0.0, vega=0.0,
            iv=sigma, intrinsic=intrinsic, time_value=0.0,
            option_type=option_type, strike=K, underlying=S,
        )

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    nd1  = _norm_cdf(d1)
    nd2  = _norm_cdf(d2)
    nnd1 = _norm_cdf(-d1)
    nnd2 = _norm_cdf(-d2)
    pdf1 = _norm_pdf(d1)

    disc = math.exp(-r * T)

    if option_type == "CALL":
        price = S * nd1 - K * disc * nd2
        delta = nd1
    else:
        price = K * disc * nnd2 - S * nnd1
        delta = nd1 - 1.0          # negative for puts

    gamma = pdf1 / (S * sigma * sqrt_T)

    # Theta: per calendar day (divide annualised by 365)
    theta_annual = (
        -(S * pdf1 * sigma) / (2.0 * sqrt_T)
        - r * K * disc * (nd2 if option_type == "CALL" else nnd2)
    )
    theta = theta_annual / 365.0

    # Vega: per 1% move in IV
    vega = S * pdf1 * sqrt_T * 0.01

    intrinsic  = max(S - K, 0.0) if option_type == "CALL" else max(K - S, 0.0)
    time_value = max(price - intrinsic, 0.0)

    half_spread = price * spread_pct
    bid = max(price - half_spread, 0.01)
    ask = price + half_spread

    return OptionPrice(
        price=round(price, 4),
        bid=round(bid,   2),
        ask=round(ask,   2),
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega,   4),
        iv=sigma,
        intrinsic=round(intrinsic,  4),
        time_value=round(time_value, 4),
        option_type=option_type,
        strike=K,
        underlying=S,
    )


# ---------------------------------------------------------------------------
# IV regime helper — approximate QQQ historical IV by year
# ---------------------------------------------------------------------------

# Rough QQQ annualised IV by calendar year based on VXN averages.
# Used when no live IV is available. Replace with real-time IV in production.
_HISTORICAL_IV = {
    2018: 0.20,
    2019: 0.16,
    2020: 0.35,   # COVID spike
    2021: 0.18,
    2022: 0.28,   # bear market
    2023: 0.18,
    2024: 0.17,
    2025: 0.18,
    2026: 0.18,
}
_DEFAULT_IV = 0.18


def get_iv_estimate(year: int) -> float:
    """Return an approximate IV for backtesting based on the calendar year."""
    return _HISTORICAL_IV.get(year, _DEFAULT_IV)


# ---------------------------------------------------------------------------
# Strike selection helpers
# ---------------------------------------------------------------------------

def select_strike(spot: float, offset_pct: float = 0.005, interval: float = 1.0) -> float:
    """
    Choose a strike slightly OTM from the spot price.

    Args:
        spot        : current underlying price
        offset_pct  : how far OTM to go (0.005 = 0.5% OTM)
        interval    : round to nearest interval (1.0 for $1 strikes, 5.0 for $5)

    Example:
        spot=481.73, offset_pct=0.005, interval=1.0  →  484.0
    """
    raw    = spot * (1.0 + offset_pct)
    strike = round(raw / interval) * interval
    return float(strike)


def days_to_nearest_expiry(target_dte: int = 1, bar_time=None) -> float:
    """
    Convert a target DTE to fractional years, accounting for intraday
    time decay when bar_time is provided.

    Without bar_time: uses the full DTE as trading days (entry use case).
    With bar_time: subtracts the fraction of the current trading day
    already elapsed so that T decreases continuously through the session.

    target_dte=0  →  same-day expiry (0DTE)
    target_dte=1  →  next day
    target_dte=7  →  next weekly

    Trading session: 09:30–16:00 ET = 6.5 hours = 390 minutes.
    """
    from datetime import time as dtime
    MARKET_OPEN_MIN  = 9 * 60 + 30    # 570
    MARKET_CLOSE_MIN = 16 * 60        # 960
    SESSION_MINUTES  = MARKET_CLOSE_MIN - MARKET_OPEN_MIN  # 390

    base_dte = max(target_dte, 0.5)

    if bar_time is not None:
        bar_min = bar_time.hour * 60 + bar_time.minute
        elapsed = max(0, bar_min - MARKET_OPEN_MIN)
        day_fraction_remaining = max(0.0, 1.0 - elapsed / SESSION_MINUTES)
        # For 0DTE: T shrinks from 0.5/252 to ~0 through the day
        # For 1DTE: T shrinks from 1/252 toward 0/252 through the day
        dte = max(target_dte - 1 + day_fraction_remaining, 0.5 / SESSION_MINUTES)
        return dte / 252.0

    return base_dte / 252.0
