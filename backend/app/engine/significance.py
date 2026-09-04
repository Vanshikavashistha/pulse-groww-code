"""What counts as a meaningful change.

The naive rule is a flat threshold: alert when a stock moves more than 2%.
It is wrong in both directions at once. MARUTI moving 2% is a genuine event;
ADANIENT moving 2% happens most afternoons. A flat threshold therefore floods
the user with noise on volatile names while staying silent on exactly the
large-cap moves they would care about most.

So significance here is measured in units of *each symbol's own recent
volatility*. A move is scored as a z-score:

    z = return / sigma_symbol

sigma is tracked as an EWMA of squared returns with lambda = 0.94, the
RiskMetrics standard. EWMA rather than a simple rolling standard deviation
because volatility clusters: after a shock, the next move genuinely is more
likely to be large, and an exponentially-weighted estimate adapts to that
within a few ticks instead of lagging a whole window behind.

Two guards keep the maths honest:

  * A floor on absolute movement. Without it, a stock that has been perfectly
    flat drives sigma toward zero and every rounding tick becomes a
    ten-sigma event.
  * A minimum sample count. Until a symbol has enough history we fall back to
    a flat percentage rule and say so, rather than pretending to a confidence
    the data does not support.

This module is deliberately pure: no database, no clock, no network. That is
what makes the judgement calls in it directly unit-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import settings

# Base weights per event type, used in the attention score. They encode a
# view: a reversal of direction changes a decision more often than a move
# that merely continues one, so it outranks it at equal magnitude.
TYPE_WEIGHT = {
    "PRICE_MOVE": 1.0,
    "TREND_REVERSAL": 1.4,
    "RANGE_BREAK": 1.3,
    "VOLUME_SURGE": 0.9,
    "STALE_DATA": 1.1,
}


@dataclass
class VolatilityState:
    """Rolling statistics for one symbol. Updated incrementally, O(1) per
    tick, so cost does not grow with history length."""
    ewma_variance: float = 0.0
    sample_count: int = 0
    avg_volume: float | None = None

    @property
    def sigma(self) -> float:
        return math.sqrt(self.ewma_variance) if self.ewma_variance > 0 else 0.0

    @property
    def is_warm(self) -> bool:
        return self.sample_count >= settings.VOLATILITY_MIN_SAMPLES


def update_volatility(state: VolatilityState, ret: float) -> VolatilityState:
    """Fold one return into the EWMA variance estimate."""
    lam = settings.VOLATILITY_EWMA_LAMBDA
    variance = (lam * state.ewma_variance + (1 - lam) * ret * ret
                if state.sample_count else ret * ret)
    return VolatilityState(
        ewma_variance=variance,
        sample_count=state.sample_count + 1,
        avg_volume=state.avg_volume,
    )


def update_avg_volume(state: VolatilityState, volume: float | None) -> VolatilityState:
    if volume is None or volume <= 0:
        return state
    avg = volume if state.avg_volume is None else 0.9 * state.avg_volume + 0.1 * volume
    return VolatilityState(state.ewma_variance, state.sample_count, avg)


def sigma_over(state: VolatilityState, horizon_ticks: int) -> float:
    """Scale per-tick volatility to the horizon the move is measured over.

    This is the correction for the mistake that makes naive versions of this
    model report impossible numbers. sigma is estimated from tick-to-tick
    returns, but the move a user cares about is cumulative since the previous
    close, which spans many ticks. Comparing a whole day's move against a
    single tick's volatility inflates the z-score by roughly the square root
    of the number of ticks, and produces readings like 38 sigma -- a figure
    that should never appear in a working system, because it means the units
    do not match rather than that anything remarkable happened.

    Under the standard random-walk assumption, variance accumulates linearly
    with time, so volatility grows with its square root:

        sigma_n = sigma_tick * sqrt(n)

    The assumption is imperfect -- real returns are fat-tailed and slightly
    autocorrelated, so this understates tail risk a little -- but it is the
    right first-order correction and it is the convention every risk desk
    uses to annualise volatility.
    """
    return state.sigma * math.sqrt(max(1, horizon_ticks))


def z_score(ret: float, state: VolatilityState, horizon_ticks: int = 1) -> float:
    """How unusual this return is for this symbol, in sigmas.

    Returns 0.0 while the estimate is still cold, which forces callers onto
    the flat-percentage fallback instead of trusting a meaningless number.
    """
    if not state.is_warm:
        return 0.0
    sigma = sigma_over(state, horizon_ticks)
    if sigma <= 0:
        return 0.0
    return ret / sigma


def is_significant_move(change_pct: float, state: VolatilityState,
                        horizon_ticks: int = 1) -> tuple[bool, float, str]:
    """Decide whether a price change deserves the user's attention.

    Returns (significant, |z|, reason) where reason explains the rule that
    fired. The reason is surfaced in the UI: a user who is told *why* a stock
    was flagged can calibrate their trust in the flagging, and a reviewer can
    tell a considered system from a magic one.
    """
    ret = change_pct / 100.0

    # Never interrupt someone over a rounding error, whatever the maths says.
    if abs(change_pct) < settings.MIN_ABS_MOVE_PCT:
        return False, 0.0, "below the minimum move floor"

    if not state.is_warm:
        # Honest fallback while we are still learning the symbol.
        flat = abs(change_pct) >= 2.0
        return flat, 0.0, "flat 2% rule (still learning this symbol's volatility)"

    z = abs(z_score(ret, state, horizon_ticks))
    if z >= settings.Z_SCORE_THRESHOLD:
        return True, z, f"{z:.1f} sigmas beyond this symbol's usual range over the same period"
    return False, z, "within this symbol's normal range"


def is_volume_surge(volume: float | None, state: VolatilityState) -> tuple[bool, float]:
    if volume is None or not state.avg_volume:
        return False, 0.0
    ratio = volume / state.avg_volume
    return ratio >= settings.VOLUME_SURGE_RATIO, ratio


def attention_score(
    signal_type: str,
    magnitude: float,
    volume_ratio: float | None,
    age_minutes: float,
    pinned: bool,
) -> float:
    """Rank the feed.

    Ordering by timestamp alone is wrong for this product: the user is not
    reading a log, they are triaging. A four-sigma move from an hour ago
    matters more than a marginal one from a minute ago, so magnitude and
    confirmation are weighed against an exponential recency decay rather than
    recency winning outright.

    Half-life decay rather than a hard cutoff because relevance fades
    smoothly; a cliff at 60 minutes would make the ordering jump for no
    reason the user could perceive.
    """
    score = settings.W_MAGNITUDE * min(magnitude, 6.0)
    score += settings.W_TYPE * TYPE_WEIGHT.get(signal_type, 1.0)

    if volume_ratio:
        # Log so that a 20x surge does not swamp every other consideration.
        score += settings.W_VOLUME * math.log1p(max(0.0, volume_ratio - 1.0))

    decay = 0.5 ** (age_minutes / settings.RECENCY_HALFLIFE_MINUTES)
    score *= (0.35 + 0.65 * decay)  # floor so old-but-huge never vanishes

    if pinned:
        score += settings.W_PIN

    return round(score, 4)


def severity_of(magnitude: float, change_pct: float) -> str:
    if magnitude >= 3.5 or abs(change_pct) >= 5:
        return "high"
    if magnitude >= 2.0 or abs(change_pct) >= 2:
        return "medium"
    return "info"
