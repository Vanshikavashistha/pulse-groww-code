"""Tests for the parts where being wrong is expensive.

Not coverage for its own sake. These cover the four places this system could
plausibly mislead a user about money: mis-scoring a move, replaying a stale
tick, alerting twice for one event, and losing an event to a race between
rendering and acknowledgement.
"""
import math
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engine.detector import ingest_quote
from app.engine.feed import build_feed, mark_seen
from app.engine.significance import (
    VolatilityState, attention_score, is_significant_move, update_volatility,
    z_score,
)
from app.models import Base, Signal, Symbol, User, WatchlistItem
from app.providers.base import Quote


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


def quote(ticker="RELIANCE", price=100.0, as_of=None, prev_close=100.0,
          volume=1_000_000.0):
    return Quote(ticker=ticker, price=price, prev_close=prev_close,
                 day_open=prev_close, day_high=price, day_low=price,
                 volume=volume, as_of=as_of or datetime.utcnow(),
                 source="test", name=ticker)


# --- the significance model ----------------------------------------------

def test_same_move_scored_differently_for_calm_and_volatile_symbols():
    """The core claim of the product: 2% is not a universal threshold."""
    calm = VolatilityState()
    for r in [0.0009, -0.0011, 0.0008, -0.0007, 0.0012, -0.0009] * 3:
        calm = update_volatility(calm, r)

    volatile = VolatilityState()
    for r in [0.019, -0.022, 0.017, -0.025, 0.021, -0.018] * 3:
        volatile = update_volatility(volatile, r)

    calm_hit, calm_z, _ = is_significant_move(2.0, calm)
    vol_hit, vol_z, _ = is_significant_move(2.0, volatile)

    assert calm_hit, "a 2% move in a low-volatility stock must be flagged"
    assert not vol_hit, "the same move in a volatile stock is ordinary"
    assert calm_z > vol_z


def test_cold_symbol_falls_back_to_flat_rule_and_says_so():
    cold = VolatilityState()
    hit, z, reason = is_significant_move(3.0, cold)
    assert hit and z == 0.0
    assert "still learning" in reason


def test_tiny_moves_never_alert_even_on_a_flat_symbol():
    """Guards against sigma collapsing toward zero on a stock that has not
    moved, which would turn every rounding tick into a ten-sigma event."""
    flat = VolatilityState()
    for _ in range(30):
        flat = update_volatility(flat, 0.000001)
    hit, _, reason = is_significant_move(0.05, flat)
    assert not hit
    assert "floor" in reason


def test_recent_and_old_signals_are_ranked_by_more_than_time():
    big_and_old = attention_score("PRICE_MOVE", 5.0, None, 120, False)
    small_and_new = attention_score("PRICE_MOVE", 2.1, None, 0, False)
    assert big_and_old > small_and_new


def test_pinned_symbols_outrank_equivalent_unpinned_ones():
    assert (attention_score("PRICE_MOVE", 2.0, None, 5, True)
            > attention_score("PRICE_MOVE", 2.0, None, 5, False))


# --- ingest invariants ----------------------------------------------------

def test_out_of_order_tick_is_rejected(session):
    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now))
    ingest_quote(session, quote(price=110.0, as_of=now + timedelta(seconds=10)))
    # A late tick from before the last accepted one arrives.
    ingest_quote(session, quote(price=50.0, as_of=now + timedelta(seconds=5)))
    session.flush()

    symbol = session.get(Symbol, "RELIANCE")
    assert symbol.last_price == 110.0, "a late tick must not rewind the price"


def test_duplicate_tick_is_idempotent(session):
    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now))
    duplicate = quote(price=108.0, as_of=now + timedelta(seconds=5))
    first = ingest_quote(session, duplicate)
    second = ingest_quote(session, duplicate)   # same payload, replayed
    session.flush()
    assert second == [], "replaying an identical tick must produce no new signals"
    assert session.get(Symbol, "RELIANCE").last_price == 108.0
    del first


def test_one_sustained_move_produces_one_signal_not_many(session):
    """A stock that stays down 4% should not re-alert on every poll."""
    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now))
    for i in range(1, 15):
        ingest_quote(session, quote(price=96.0 + i * 0.001,
                                    as_of=now + timedelta(seconds=i * 5)))
    session.flush()

    price_moves = session.scalars(
        select(Signal).where(Signal.type == "PRICE_MOVE")).all()
    assert len(price_moves) <= 2, f"expected suppression, got {len(price_moves)}"


def test_reversal_is_detected_when_crossing_previous_close(session):
    now = datetime.utcnow()
    ingest_quote(session, quote(price=103.0, as_of=now, prev_close=100.0))
    ingest_quote(session, quote(price=97.0, as_of=now + timedelta(seconds=5),
                                prev_close=100.0))
    session.flush()
    types = {s.type for s in session.scalars(select(Signal)).all()}
    assert "TREND_REVERSAL" in types


# --- the read-state race --------------------------------------------------

def test_signal_arriving_during_acknowledgement_stays_unread(session):
    """The race this design exists to survive.

    The user renders a feed ending at signal 5, a signal 6 is detected while
    the acknowledgement is in flight, and the client then acknowledges 5.
    Signal 6 must survive.
    """
    user = User(handle="race")
    session.add(user)
    session.flush()
    session.add(WatchlistItem(user_id=user.id, ticker="RELIANCE"))

    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now))
    ingest_quote(session, quote(price=94.0, as_of=now + timedelta(seconds=5)))
    session.flush()

    feed = build_feed(session, user.id)
    rendered_cursor = feed["ack_cursor"]

    # A new signal lands before the client's acknowledgement arrives.
    ingest_quote(session, quote(price=112.0, as_of=now + timedelta(minutes=30)))
    session.flush()

    mark_seen(session, user.id, rendered_cursor)
    session.flush()

    after = build_feed(session, user.id)
    assert after["unread_count"] >= 1, "the in-flight signal was swallowed"


def test_cursor_only_moves_forward(session):
    user = User(handle="monotonic")
    session.add(user)
    session.flush()
    session.add(WatchlistItem(user_id=user.id, ticker="RELIANCE",
                              last_seen_signal_id=50))
    session.flush()

    mark_seen(session, user.id, ack_cursor=10)   # a stale acknowledgement
    session.flush()
    item = session.scalar(select(WatchlistItem).where(
        WatchlistItem.user_id == user.id))
    assert item.last_seen_signal_id == 50


def test_personal_drift_is_measured_from_what_the_user_actually_saw(session):
    """Two users, same stock, different last-visit prices, different stories."""
    early = User(handle="early")
    late = User(handle="late")
    session.add_all([early, late])
    session.flush()

    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now, prev_close=100.0))
    session.flush()

    session.add(WatchlistItem(user_id=early.id, ticker="RELIANCE",
                              last_seen_price=100.0, last_seen_at=now,
                              last_seen_signal_id=9999))
    session.flush()

    ingest_quote(session, quote(price=104.0, as_of=now + timedelta(minutes=1),
                                prev_close=100.0))
    session.flush()

    session.add(WatchlistItem(user_id=late.id, ticker="RELIANCE",
                              last_seen_price=104.0,
                              last_seen_at=now + timedelta(minutes=1),
                              last_seen_signal_id=9999))
    session.flush()

    early_feed = build_feed(session, early.id)
    late_feed = build_feed(session, late.id)

    early_drift = [e for e in early_feed["entries"] if e["kind"] == "drift"]
    late_drift = [e for e in late_feed["entries"] if e["kind"] == "drift"]

    assert early_drift and abs(early_drift[0]["change_pct"] - 4.0) < 0.1
    assert not late_drift, "the user who just looked has seen nothing new"


# --- horizon matching -----------------------------------------------------

def test_cumulative_move_is_judged_against_matched_horizon_volatility():
    """The bug this scaling exists to prevent.

    sigma is estimated from tick-to-tick returns, but the move a user sees is
    cumulative over many ticks. Judging one against the other inflates the
    z-score by roughly sqrt(ticks) and produces impossible readings. Scaling
    by the square root of time is what keeps the units honest.
    """
    state = VolatilityState()
    for r in [0.001, -0.0012, 0.0009, -0.0011, 0.0013, -0.0008] * 4:
        state = update_volatility(state, r)

    naive = abs(z_score(0.03, state, horizon_ticks=1))
    matched = abs(z_score(0.03, state, horizon_ticks=240))

    assert naive > 20, "a one-tick horizon should look absurd, that is the bug"
    assert matched < 3, "scaled to a session, a 3% move is a normal-sized event"
    # sigma grows with the square root of the horizon, so the z-score shrinks
    # by the same factor.
    assert abs(naive / matched - math.sqrt(240)) < 0.01


def test_range_break_needs_a_real_margin(session):
    """A running high is beaten almost every tick in a trend. Only a break
    that clears the old extreme by a margin is worth telling anyone about."""
    now = datetime.utcnow()
    ingest_quote(session, quote(price=100.0, as_of=now))
    for i in range(1, 20):  # warm the symbol up
        ingest_quote(session, quote(price=100.0 + i * 0.005,
                                    as_of=now + timedelta(seconds=i)))
    session.flush()
    before = len(session.scalars(
        select(Signal).where(Signal.type == "RANGE_BREAK")).all())

    # A new high, but by a trivial amount.
    ingest_quote(session, quote(price=100.11, as_of=now + timedelta(seconds=60)))
    session.flush()
    after = len(session.scalars(
        select(Signal).where(Signal.type == "RANGE_BREAK")).all())

    assert after == before, "a hairline new high is not news"
