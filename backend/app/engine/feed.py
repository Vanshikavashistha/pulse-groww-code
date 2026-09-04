"""Assembling one user's answer to "what changed since I last looked?".

There are two genuinely different kinds of change, and conflating them is why
most watchlists cannot answer the question:

  Discrete events are shared. A volume surge in ZOMATO is the same fact for
  every user watching it, so it is detected once and stored once. Reading it
  is a cursor comparison: signal.id > my last_seen_signal_id.

  Continuous drift is personal. The same stock, at the same price, is a
  different message depending on when you last looked. Down 0.4% since a user
  checked ten minutes ago is nothing; down 0.4% since they checked on Monday
  means the last three days were flat, which is itself informative. This is
  measured from the price that user actually had on screen, not from the
  previous close.

The feed is the union of the two, ranked by attention score.

On fan-out versus fan-in: signals are computed once and joined to users at
read time (fan-in). The alternative -- writing a copy of every signal into
every subscriber's inbox at detection time (fan-out) -- would make reads
trivial but multiply writes by the number of users watching a symbol, which
for a popular ticker on a broking platform is the entire user base. Reads
here are cheap anyway: an index on (ticker, id) turns the unread query into a
range scan. The crossover point where fan-out wins is analysed in DESIGN.md.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Signal, Symbol, WatchlistItem
from .significance import attention_score


def _minutes_since(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    return max(0.0, (datetime.utcnow() - moment).total_seconds() / 60.0)


def build_feed(session: Session, user_id: int) -> dict:
    items = session.scalars(
        select(WatchlistItem).where(WatchlistItem.user_id == user_id)).all()
    if not items:
        return {"since": None, "cursor": 0, "ack_cursor": 0, "unread_count": 0,
                "entries": [], "quiet": True}

    by_ticker = {item.ticker: item for item in items}
    symbols = {
        s.ticker: s for s in session.scalars(
            select(Symbol).where(Symbol.ticker.in_(list(by_ticker)))).all()
    }

    # The user's overall reading position: the oldest per-symbol cursor, so a
    # symbol added recently does not hide events on the rest of the list.
    cursor = min((item.last_seen_signal_id or 0) for item in items)
    since = min((item.last_seen_at for item in items if item.last_seen_at),
                default=None)

    entries: list[dict] = []

    # --- part one: unread shared signals ---------------------------------
    unread = session.scalars(
        select(Signal)
        .where(Signal.ticker.in_(list(by_ticker)))
        .order_by(Signal.id.desc())
        .limit(settings.FEED_MAX_ITEMS * 3)
    ).all()

    for signal in unread:
        item = by_ticker[signal.ticker]
        if signal.id <= (item.last_seen_signal_id or 0):
            continue  # this user has already seen it
        symbol = symbols.get(signal.ticker)
        entries.append({
            "kind": "event",
            "signal_id": signal.id,
            "ticker": signal.ticker,
            "name": symbol.name if symbol else signal.ticker,
            "type": signal.type,
            "headline": signal.headline,
            "detail": signal.detail,
            "severity": signal.severity,
            "change_pct": signal.change_pct,
            "magnitude": signal.magnitude,
            "price": signal.price_at_signal,
            "at": signal.created_at.isoformat(),
            "score": attention_score(
                signal.type, signal.magnitude, signal.volume_ratio,
                _minutes_since(signal.created_at), item.pinned),
        })

    # --- part two: personal drift since this user's last visit ------------
    # Only for symbols that did not already produce an event, so the feed
    # says each thing once.
    tickers_with_events = {e["ticker"] for e in entries}
    for ticker, item in by_ticker.items():
        if ticker in tickers_with_events:
            continue
        symbol = symbols.get(ticker)
        if not symbol or not symbol.last_price or not item.last_seen_price:
            continue

        drift_pct = (symbol.last_price - item.last_seen_price) / item.last_seen_price * 100
        if abs(drift_pct) < settings.MIN_ABS_MOVE_PCT:
            continue

        gap = _minutes_since(item.last_seen_at)
        direction = "up" if drift_pct > 0 else "down"
        entries.append({
            "kind": "drift",
            "signal_id": None,
            "ticker": ticker,
            "name": symbol.name,
            "type": "PERSONAL_DRIFT",
            "headline": f"{ticker} {direction} {abs(drift_pct):.2f}% since you last looked",
            "detail": (f"You last saw it at {item.last_seen_price:,.2f}"
                       f"{_gap_phrase(gap)}. It is now {symbol.last_price:,.2f}."),
            "severity": "medium" if abs(drift_pct) >= 2 else "info",
            "change_pct": round(drift_pct, 3),
            "magnitude": min(abs(drift_pct) / 2, 4.0),
            "price": symbol.last_price,
            "at": (symbol.as_of or datetime.utcnow()).isoformat(),
            "score": attention_score("PRICE_MOVE", min(abs(drift_pct) / 2, 4.0),
                                     None, 0.0, item.pinned),
        })

    entries.sort(key=lambda e: e["score"], reverse=True)
    entries = entries[:settings.FEED_MAX_ITEMS]

    max_signal_id = session.scalar(select(Signal.id).order_by(Signal.id.desc())) or 0

    return {
        "since": since.isoformat() if since else None,
        "cursor": cursor,
        # The client sends this back when marking as read. Using the id the
        # user actually saw -- rather than "now" on the server -- means a
        # signal arriving between render and acknowledgement stays unread
        # instead of being silently swallowed.
        "ack_cursor": max_signal_id,
        "unread_count": len(entries),
        "entries": entries,
        "quiet": len(entries) == 0,
    }


def _gap_phrase(minutes: float) -> str:
    if minutes < 1:
        return ""
    if minutes < 60:
        return f", about {int(minutes)} minutes ago"
    if minutes < 60 * 24:
        return f", about {int(minutes // 60)} hours ago"
    return f", about {int(minutes // 1440)} days ago"


def mark_seen(session: Session, user_id: int, ack_cursor: int,
              tickers: list[str] | None = None) -> int:
    """Advance the user's reading position.

    Deliberately not `UPDATE ... SET cursor = (SELECT MAX(id) FROM signals)`.
    That would consume signals the user never had on screen. The client passes
    back the highest id it actually rendered, so anything detected during the
    round trip survives to the next visit. The cursor also only ever moves
    forward, which makes a duplicated or out-of-order acknowledgement a no-op
    rather than a regression.
    """
    query = select(WatchlistItem).where(WatchlistItem.user_id == user_id)
    if tickers:
        query = query.where(WatchlistItem.ticker.in_(tickers))

    now = datetime.utcnow()
    updated = 0
    for item in session.scalars(query).all():
        symbol = session.get(Symbol, item.ticker)
        if ack_cursor > (item.last_seen_signal_id or 0):
            item.last_seen_signal_id = ack_cursor
        if symbol and symbol.last_price:
            item.last_seen_price = symbol.last_price
        item.last_seen_at = now
        updated += 1
    return updated
