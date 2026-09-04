"""Turning quotes into signals.

Ingest is the only place that writes prices, and it enforces two invariants
that the rest of the system then gets to assume:

  1. Monotonic time. A quote is applied only if its `as_of` is newer than the
     one already stored. Providers replay, retry and reorder; without this
     guard a late-arriving tick would rewind the price, and worse, generate a
     phantom signal for a move that never happened.

  2. Idempotent signalling. Every signal carries a fingerprint built from the
     symbol, the event type and a *quantised* magnitude bucket. A stock that
     is down four sigma stays down four sigma across the next dozen polls;
     without quantisation the user gets the same alert twelve times. The
     unique index on fingerprint makes duplicate suppression a database
     guarantee rather than an application-level hope, which matters once more
     than one poller runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Signal, Symbol
from ..providers.base import Quote
from .significance import (
    VolatilityState, is_significant_move, is_volume_surge, severity_of,
    update_avg_volume, update_volatility,
)

log = logging.getLogger(__name__)

SPARK_POINTS = 40  # enough to read a shape, small enough to send every poll


def _fingerprint(ticker: str, signal_type: str, bucket: str) -> str:
    raw = f"{ticker}|{signal_type}|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def _band(change_pct: float) -> str:
    """Quantise a move into a coarse band.

    Fingerprinting on the exact percentage meant a stock grinding from 3% to
    4% produced a fresh alert at every step, which is precisely the flooding
    this product exists to prevent. Bands mean the user hears "this is
    moving" once, and hears again only when it has moved to a materially
    different place.
    """
    step = 3.0
    return f"{'+' if change_pct >= 0 else '-'}{int(abs(change_pct) // step)}"


def _cooldown_bucket(now: datetime) -> int:
    """Quantise time so one ongoing condition yields one signal per window."""
    return int(now.timestamp() // settings.SIGNAL_COOLDOWN_SECONDS)


def _state_of(symbol: Symbol) -> VolatilityState:
    return VolatilityState(
        ewma_variance=symbol.ewma_variance or 0.0,
        sample_count=symbol.sample_count or 0,
        avg_volume=symbol.avg_volume,
    )


def ingest_quote(session: Session, quote: Quote, now: datetime | None = None,
                 emit: bool = True) -> list[Signal]:
    """Apply one quote and emit any signals it justifies.

    `now` is injectable so a warmup replay can lay down history against a
    synthetic clock rather than stamping several hundred backfilled events
    with the current time. `emit=False` updates the statistics without
    producing signals, which is how the warmup builds a volatility estimate
    before it starts reporting anything.
    """
    symbol = session.get(Symbol, quote.ticker)
    if symbol is None:
        symbol = Symbol(ticker=quote.ticker, name=quote.name or quote.ticker)
        session.add(symbol)
        session.flush()

    if quote.name and not symbol.name:
        symbol.name = quote.name

    # --- invariant 1: reject stale and duplicate ticks --------------------
    if symbol.as_of is not None and quote.as_of <= symbol.as_of:
        log.debug("dropping out-of-order tick for %s", quote.ticker)
        symbol.fetched_at = datetime.utcnow()   # we did hear from upstream
        return []

    previous_price = symbol.last_price
    prior_high = symbol.window_high
    prior_low = symbol.window_low
    signals: list[Signal] = []
    state = _state_of(symbol)

    if previous_price and previous_price > 0:
        tick_return = (quote.price - previous_price) / previous_price
        state = update_volatility(state, tick_return)
    state = update_avg_volume(state, quote.volume)

    # --- write the accepted quote ----------------------------------------
    symbol.last_price = quote.price
    symbol.prev_close = quote.prev_close or symbol.prev_close
    symbol.day_open = quote.day_open or symbol.day_open
    symbol.day_high = quote.day_high or symbol.day_high
    symbol.day_low = quote.day_low or symbol.day_low
    symbol.volume = quote.volume
    symbol.as_of = quote.as_of
    symbol.fetched_at = datetime.utcnow()
    symbol.source = quote.source
    symbol.is_delayed = quote.is_delayed
    symbol.ewma_variance = state.ewma_variance
    symbol.sample_count = state.sample_count
    symbol.ticks_since_open = (symbol.ticks_since_open or 0) + 1
    symbol.avg_volume = state.avg_volume
    symbol.stale_notified_at = None

    # Ring buffer for the sparkline, capped at SPARK_POINTS.
    try:
        history = json.loads(symbol.recent_prices) if symbol.recent_prices else []
    except (ValueError, TypeError):
        history = []
    history.append(round(quote.price, 2))
    symbol.recent_prices = json.dumps(history[-SPARK_POINTS:])

    symbol.window_high = max(symbol.window_high or quote.price, quote.price)
    symbol.window_low = min(symbol.window_low or quote.price, quote.price)

    if previous_price is None:
        return []  # first sighting of a symbol is not news

    now = now or datetime.utcnow()
    bucket = _cooldown_bucket(now)

    # --- detector 1: an unusual move -------------------------------------
    # Measured against the previous close, which is the number the user's
    # mental model is anchored on, but judged against the symbol's own
    # volatility rather than a flat threshold.
    reference = symbol.prev_close or previous_price
    change_pct = (quote.price - reference) / reference * 100
    # The move spans every tick since the reference close, so the volatility
    # it is judged against must span the same period. Without this the
    # z-score is inflated by roughly sqrt(ticks) and reports impossibilities.
    horizon = min(max(1, symbol.ticks_since_open or 1),
                  settings.HORIZON_CAP_TICKS)
    significant, magnitude, reason = is_significant_move(change_pct, state, horizon)

    if significant:
        direction = "up" if change_pct > 0 else "down"
        signals.append(_build(
            symbol, "PRICE_MOVE",
            headline=f"{symbol.ticker} {direction} {abs(change_pct):.2f}%",
            detail=f"Now {quote.price:,.2f}, {reason}.",
            magnitude=magnitude, change_pct=change_pct, price=quote.price,
            bucket=f"{bucket}:{_band(change_pct)}:{int(min(magnitude, 6))}",
        ))

    # --- detector 2: direction flipped since the open --------------------
    # A stock crossing back through its previous close is a different piece of
    # information from a stock simply continuing to fall, and it is the one
    # most likely to change what a user does next.
    if symbol.prev_close and previous_price:
        was_up = previous_price >= symbol.prev_close
        is_up = quote.price >= symbol.prev_close
        if was_up != is_up and abs(change_pct) >= settings.MIN_ABS_MOVE_PCT:
            signals.append(_build(
                symbol, "TREND_REVERSAL",
                headline=f"{symbol.ticker} flipped to {'green' if is_up else 'red'}",
                detail=(f"Crossed its previous close of {symbol.prev_close:,.2f} "
                        f"and is now {change_pct:+.2f}% on the day."),
                magnitude=max(magnitude, 1.5), change_pct=change_pct,
                price=quote.price, bucket=f"{bucket}:{is_up}",
            ))

    # --- detector 3: volume confirmation ---------------------------------
    surged, ratio = is_volume_surge(quote.volume, state)
    if surged:
        signals.append(_build(
            symbol, "VOLUME_SURGE",
            headline=f"Unusual volume in {symbol.ticker}",
            detail=f"Trading at {ratio:.1f}x its typical volume for this period.",
            magnitude=min(ratio, 6.0), change_pct=change_pct,
            price=quote.price, bucket=f"{bucket}:{round(ratio)}",
            volume_ratio=ratio,
        ))

    # --- detector 4: breaking its recent range ---------------------------
    if symbol.sample_count > settings.VOLATILITY_MIN_SAMPLES:
        margin = settings.RANGE_BREAK_MARGIN_PCT / 100
        if (prior_high and quote.price > prior_high * (1 + margin)
                and previous_price <= prior_high * (1 + margin)):
            signals.append(_build(
                symbol, "RANGE_BREAK",
                headline=f"{symbol.ticker} at a session high",
                detail=(f"Passed its previous session high of {prior_high:,.2f} "
                        f"to reach {quote.price:,.2f}."),
                magnitude=max(magnitude, 2.0), change_pct=change_pct,
                price=quote.price, bucket=f"{bucket}:high",
            ))
        elif (prior_low and quote.price < prior_low * (1 - margin)
                and previous_price >= prior_low * (1 - margin)):
            signals.append(_build(
                symbol, "RANGE_BREAK",
                headline=f"{symbol.ticker} at a session low",
                detail=(f"Passed its previous session low of {prior_low:,.2f} "
                        f"to reach {quote.price:,.2f}."),
                magnitude=max(magnitude, 2.0), change_pct=change_pct,
                price=quote.price, bucket=f"{bucket}:low",
            ))

    if not emit:
        return []
    return _persist(session, signals, now)


def _build(symbol: Symbol, signal_type: str, headline: str, detail: str,
           magnitude: float, change_pct: float, price: float, bucket: str,
           volume_ratio: float | None = None) -> Signal:
    return Signal(
        ticker=symbol.ticker,
        type=signal_type,
        headline=headline,
        detail=detail,
        magnitude=round(magnitude, 3),
        change_pct=round(change_pct, 3),
        volume_ratio=round(volume_ratio, 2) if volume_ratio else None,
        severity=severity_of(magnitude, change_pct),
        price_at_signal=price,
        fingerprint=_fingerprint(symbol.ticker, signal_type, bucket),
    )


def _persist(session: Session, signals: list[Signal],
             now: datetime | None = None) -> list[Signal]:
    """Insert signals, letting the unique index reject duplicates.

    Each insert is savepointed so that one rejected duplicate does not abort
    the surrounding transaction and lose the accepted price update with it.
    """
    written: list[Signal] = []
    for signal in signals:
        if now is not None:
            signal.created_at = now
        try:
            with session.begin_nested():
                session.add(signal)
            written.append(signal)
        except IntegrityError:
            log.debug("suppressed duplicate signal %s", signal.fingerprint)
    return written


def emit_stale_signal(session: Session, symbol: Symbol) -> Signal | None:
    """Tell the user we have gone blind on a symbol.

    Surfacing our own failure is a deliberate product choice. In a financial
    interface an unlabelled stale price is worse than a missing one: the user
    cannot tell the difference between a stock that has not moved and a feed
    that has stopped, and only one of those should change their behaviour.
    """
    now = datetime.utcnow()
    if symbol.stale_notified_at and now - symbol.stale_notified_at < timedelta(
            seconds=settings.SIGNAL_COOLDOWN_SECONDS):
        return None

    symbol.stale_notified_at = now
    age = int((now - symbol.fetched_at).total_seconds()) if symbol.fetched_at else 0
    signal = Signal(
        ticker=symbol.ticker,
        type="STALE_DATA",
        headline=f"{symbol.ticker} price may be out of date",
        detail=(f"No fresh quote for {age // 60}m {age % 60}s. "
                f"Showing the last known price of {symbol.last_price:,.2f}."),
        magnitude=1.0, change_pct=0.0, severity="warning",
        price_at_signal=symbol.last_price,
        fingerprint=_fingerprint(symbol.ticker, "STALE_DATA",
                                 str(_cooldown_bucket(now))),
    )
    written = _persist(session, [signal])
    return written[0] if written else None
