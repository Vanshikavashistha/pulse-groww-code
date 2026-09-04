"""Database schema.

The whole product rests on a two-layer model of "what changed":

  Layer 1 - Signal (shared, append-only).
      A discrete market event on a symbol. Computed ONCE per symbol by the
      poller, never per user. A volume surge in TATAMOTORS is the same event
      for every user holding it, so it is stored once and read by many.

  Layer 2 - WatchlistItem.last_seen_* (per user, per symbol).
      The user's personal reading position: the price they last saw and the
      id of the last signal they had rendered. This is what makes the answer
      to "what changed?" different for someone returning after five minutes
      versus five days.

Splitting these is the central design decision of the system. See DESIGN.md.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class User(Base):
    """Minimal identity. Auth is deliberately out of scope (see DESIGN.md);
    a session header carries the user so the multi-user data model is real
    and testable without building a login flow that adds no signal."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    handle = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    items = relationship("WatchlistItem", back_populates="user",
                         cascade="all, delete-orphan")


class Symbol(Base):
    """A tradeable instrument plus the rolling statistics used to decide
    whether a move on it is unusual. Storing the stats here (rather than
    recomputing from a tick archive) keeps the hot path O(1) per symbol."""
    __tablename__ = "symbols"

    ticker = Column(String(24), primary_key=True)
    name = Column(String(128), nullable=False, default="")
    exchange = Column(String(16), nullable=False, default="NSE")

    # Latest known quote. Written only when as_of moves forward, which is how
    # out-of-order and duplicated upstream ticks are rejected.
    last_price = Column(Float, nullable=True)
    prev_close = Column(Float, nullable=True)
    day_open = Column(Float, nullable=True)
    day_high = Column(Float, nullable=True)
    day_low = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)

    as_of = Column(DateTime, nullable=True)       # exchange timestamp
    fetched_at = Column(DateTime, nullable=True)  # when we received it
    source = Column(String(24), nullable=True)
    is_delayed = Column(Boolean, default=False, nullable=False)

    # Rolling statistics, updated incrementally on each accepted tick.
    ewma_variance = Column(Float, default=0.0, nullable=False)
    sample_count = Column(Integer, default=0, nullable=False)
    avg_volume = Column(Float, nullable=True)
    # Ticks observed since the current previous-close reference. The move a
    # user sees spans this many ticks, so it is the horizon the volatility
    # estimate has to be scaled to.
    ticks_since_open = Column(Integer, default=0, nullable=False)
    window_high = Column(Float, nullable=True)
    window_low = Column(Float, nullable=True)

    # A short ring buffer of recent prices, JSON encoded. Capped, because the
    # UI needs a shape to draw, not an archive to query.
    recent_prices = Column(Text, nullable=True)

    # Set when the circuit breaker or a fetch failure leaves us blind on this
    # symbol, so the UI can say so instead of quietly showing an old number.
    stale_notified_at = Column(DateTime, nullable=True)


class WatchlistItem(Base):
    """A user's interest in a symbol, and their reading position on it."""
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker", name="uq_user_ticker"),
        Index("ix_watchlist_user", "user_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    ticker = Column(String(24), ForeignKey("symbols.ticker"), nullable=False)
    pinned = Column(Boolean, default=False, nullable=False)
    added_at = Column(DateTime, default=utcnow, nullable=False)

    # --- the reading position -------------------------------------------
    # Price the user actually had on screen at their last visit. The personal
    # delta ("down 3.1% since you last looked") is measured from here, NOT
    # from the previous close, which is what every other watchlist shows.
    last_seen_price = Column(Float, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    # Cursor into the shared signal log. Everything above it is unread.
    last_seen_signal_id = Column(Integer, default=0, nullable=False)

    user = relationship("User", back_populates="items")


class Signal(Base):
    """An immutable, shared market event.

    Append-only by design: a signal is a historical fact, so it is never
    mutated or deleted on read. "Marking as seen" moves a per-user cursor
    instead of touching this table, which keeps reads and writes from
    contending and makes the unread computation a simple id comparison.
    """
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_ticker_id", "ticker", "id"),
        Index("ix_signals_created", "created_at"),
        UniqueConstraint("fingerprint", name="uq_signal_fingerprint"),
    )

    id = Column(Integer, primary_key=True)  # monotonic -> usable as a cursor
    ticker = Column(String(24), ForeignKey("symbols.ticker"), nullable=False)
    type = Column(String(32), nullable=False)
    headline = Column(String(240), nullable=False)
    detail = Column(String(480), nullable=False, default="")

    magnitude = Column(Float, default=0.0, nullable=False)   # |z| score
    change_pct = Column(Float, default=0.0, nullable=False)
    volume_ratio = Column(Float, nullable=True)
    severity = Column(String(16), default="info", nullable=False)

    price_at_signal = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    # Deduplication key. Quantised so that one continuing move produces one
    # signal rather than one per poll.
    fingerprint = Column(String(64), nullable=False)
