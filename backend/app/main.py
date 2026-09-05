"""HTTP surface.

Identity comes from an `X-User` header. Real authentication is out of scope
for a 72-hour build and would demonstrate nothing this brief asks about, but
the data model is genuinely multi-tenant: every query is scoped by user and
two browsers with different handles see different unread state. That was
worth building, because the per-user reading position is the product.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, init_db
from .engine.feed import build_feed, mark_seen
from .engine.significance import VolatilityState, z_score
from .engine.poller import Poller
from .models import Signal, Symbol, User, WatchlistItem
from .providers.replay import UNIVERSE, ReplayProvider
from .providers.yahoo import YahooProvider
from .schemas import AddSymbolRequest, MarkSeenRequest, PinRequest

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pulse")

DEFAULT_WATCHLIST = ["RELIANCE", "HDFCBANK", "TATAMOTORS", "ZOMATO", "INFY"]


def build_provider():
    if settings.PROVIDER == "yahoo":
        return YahooProvider(timeout=settings.PROVIDER_TIMEOUT_SECONDS)
    return ReplayProvider()


poller = Poller(build_provider())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    poller.start()
    yield
    poller.stop()


app = FastAPI(title="Pulse", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- identity -------------------------------------------------------------
def current_user(db: Session = Depends(get_db),
                 x_user: str = Header(default="demo")) -> User:
    """Get-or-create, made safe against a concurrent first request.

    The client loads the feed and the watchlist in parallel, so a brand new
    handle arrives on two requests at once and both find no user. Without a
    guard, both insert and the loser dies on the unique constraint. The
    database is the arbiter: whoever loses the insert rolls back and re-reads
    the row the winner just committed.
    """
    handle = (x_user or "demo").strip()[:64] or "demo"

    user = db.scalar(select(User).where(User.handle == handle))
    if user is not None:
        return user

    try:
        user = User(handle=handle)
        db.add(user)
        db.flush()
        # A brand new user starts with a populated list so the product can be
        # judged immediately; an empty screen would demonstrate nothing.
        for ticker in DEFAULT_WATCHLIST:
            _ensure_symbol(db, ticker)
            db.add(WatchlistItem(user_id=user.id, ticker=ticker))
        db.commit()
        return user
    except IntegrityError:
        # A concurrent request created this user first. That is the expected
        # outcome of the race, not an error worth surfacing.
        db.rollback()
        user = db.scalar(select(User).where(User.handle == handle))
        if user is None:
            raise HTTPException(503, "Could not initialise your session, please retry")
        return user


def _ensure_symbol(db: Session, ticker: str) -> Symbol:
    symbol = db.get(Symbol, ticker)
    if symbol is None:
        name = UNIVERSE.get(ticker, (ticker,))[0]
        symbol = Symbol(ticker=ticker, name=name)
        db.add(symbol)
        db.flush()
    return symbol


# --- watchlist ------------------------------------------------------------
@app.get("/api/watchlist")
def get_watchlist(db: Session = Depends(get_db), user: User = Depends(current_user)):
    items = db.scalars(
        select(WatchlistItem).where(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.pinned.desc(), WatchlistItem.added_at)).all()
    if not items:
        return {"items": [], "as_of": datetime.utcnow().isoformat()}

    symbols = {s.ticker: s for s in db.scalars(
        select(Symbol).where(Symbol.ticker.in_([i.ticker for i in items]))).all()}

    now = datetime.utcnow()
    rows = []
    for item in items:
        symbol = symbols.get(item.ticker)
        if symbol is None:
            continue
        age = (now - symbol.fetched_at).total_seconds() if symbol.fetched_at else None
        change_pct = None
        if symbol.last_price and symbol.prev_close:
            change_pct = (symbol.last_price - symbol.prev_close) / symbol.prev_close * 100

        drift_pct = None
        if symbol.last_price and item.last_seen_price:
            drift_pct = (symbol.last_price - item.last_seen_price) / item.last_seen_price * 100

        # The live significance reading. This is the number the whole product
        # turns on, so the interface shows it directly rather than hiding it
        # behind a colour: how far is this symbol from its own normal range,
        # right now, in sigmas.
        state = VolatilityState(symbol.ewma_variance or 0.0,
                                symbol.sample_count or 0, symbol.avg_volume)
        live_z = (abs(z_score((change_pct or 0) / 100, state,
                              min(max(1, symbol.ticks_since_open or 1),
                                  settings.HORIZON_CAP_TICKS)))
                  if change_pct is not None else 0.0)

        try:
            spark = json.loads(symbol.recent_prices) if symbol.recent_prices else []
        except (ValueError, TypeError):
            spark = []

        rows.append({
            "ticker": symbol.ticker,
            "name": symbol.name,
            "price": symbol.last_price,
            "prev_close": symbol.prev_close,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "drift_pct": round(drift_pct, 2) if drift_pct is not None else None,
            "volume": symbol.volume,
            "pinned": item.pinned,
            "as_of": symbol.as_of.isoformat() if symbol.as_of else None,
            # Staleness is a first-class field, not an inference the client is
            # left to make. If we are unsure about a price we say so.
            "is_stale": age is not None and age > settings.STALE_AFTER_SECONDS,
            "is_delayed": symbol.is_delayed,
            "age_seconds": int(age) if age is not None else None,
            "source": symbol.source,
            "z": round(live_z, 2),
            "warm": state.is_warm,
            "spark": spark,
        })
    return {"items": rows, "as_of": now.isoformat(),
            "z_threshold": settings.Z_SCORE_THRESHOLD}


@app.post("/api/watchlist", status_code=201)
def add_symbol(payload: AddSymbolRequest, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    ticker = payload.ticker.strip().upper()
    if not ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(400, "Ticker contains unsupported characters")

    existing = db.scalar(select(WatchlistItem).where(
        WatchlistItem.user_id == user.id, WatchlistItem.ticker == ticker))
    if existing:
        raise HTTPException(409, f"{ticker} is already on your watchlist")

    _ensure_symbol(db, ticker)
    db.add(WatchlistItem(user_id=user.id, ticker=ticker))
    db.commit()
    return {"ticker": ticker, "status": "added"}


@app.delete("/api/watchlist/{ticker}")
def remove_symbol(ticker: str, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    item = db.scalar(select(WatchlistItem).where(
        WatchlistItem.user_id == user.id, WatchlistItem.ticker == ticker.upper()))
    if item is None:
        raise HTTPException(404, f"{ticker.upper()} is not on your watchlist")
    db.delete(item)
    db.commit()
    return {"ticker": ticker.upper(), "status": "removed"}


@app.post("/api/watchlist/{ticker}/pin")
def pin_symbol(ticker: str, payload: PinRequest, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    item = db.scalar(select(WatchlistItem).where(
        WatchlistItem.user_id == user.id, WatchlistItem.ticker == ticker.upper()))
    if item is None:
        raise HTTPException(404, f"{ticker.upper()} is not on your watchlist")
    item.pinned = payload.pinned
    db.commit()
    return {"ticker": item.ticker, "pinned": item.pinned}


# --- the feed -------------------------------------------------------------
@app.get("/api/feed")
def get_feed(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return build_feed(db, user.id)


@app.post("/api/feed/seen")
def post_seen(payload: MarkSeenRequest, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    updated = mark_seen(db, user.id, payload.ack_cursor, payload.tickers)
    db.commit()
    return {"updated": updated, "cursor": payload.ack_cursor}


# --- realtime -------------------------------------------------------------
@app.get("/api/stream")
async def stream(request: Request, x_user: str = Header(default="demo")):
    """Server-sent events.

    SSE rather than WebSockets: the data flow here is strictly server to
    client, and SSE gives automatic browser reconnection, ordinary HTTP
    semantics and no extra protocol to debug under time pressure. A
    WebSocket would buy bidirectional messaging this product does not need.

    The stream pushes a version marker rather than the payload itself. The
    client then re-fetches the feed through the normal endpoint, so there is
    exactly one code path that assembles a feed, and a client that missed a
    push while backgrounded converges on the next poll instead of holding a
    permanently wrong view.
    """
    async def event_source():
        last_signal_id = -1
        heartbeat = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                from .db import SessionLocal
                session = SessionLocal()
                try:
                    latest = session.scalar(
                        select(Signal.id).order_by(Signal.id.desc())) or 0
                finally:
                    session.close()

                if latest != last_signal_id:
                    last_signal_id = latest
                    payload = json.dumps({"latest_signal_id": latest,
                                          "at": datetime.utcnow().isoformat()})
                    yield f"event: update\ndata: {payload}\n\n"

                heartbeat += 1
                if heartbeat % 15 == 0:
                    # Keeps proxies from closing an idle connection.
                    yield ": keepalive\n\n"
            except Exception as exc:      # a broken stream must not 500 the app
                log.warning("stream error: %s", exc)
            await asyncio.sleep(2)

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --- operations -----------------------------------------------------------
@app.get("/api/health")
def health():
    """Exposed because a system that hides its own degradation cannot be
    trusted with money. The UI reads this to show provider state honestly."""
    return {"status": "ok", "poller": poller.health(),
            "frontend": str(STATIC_DIR) if STATIC_DIR else "not found",
            "thresholds": {"z_score": settings.Z_SCORE_THRESHOLD,
                           "volume_surge": settings.VOLUME_SURGE_RATIO,
                           "stale_after_seconds": settings.STALE_AFTER_SECONDS}}


@app.get("/api/universe")
def universe():
    """Symbols the demo provider knows about, for the add-symbol picker."""
    return {"symbols": [{"ticker": t, "name": v[0]} for t, v in UNIVERSE.items()]}


# --- serving the built frontend -------------------------------------------
#
# In development the Vite dev server proxies /api to this process. In
# production the two are one service: FastAPI serves the built React bundle
# from the same origin as the API.
#
# One origin rather than two is the simpler deployment by some distance --
# no CORS, no second host to configure, no chance of the frontend pointing at
# a stale backend URL. The cost is that a frontend change needs a rebuild,
# which for a project of this size is a few seconds.
#
# The bundle's location differs between running from a container image and
# running from a checkout, so rather than hardcoding one layout we check the
# plausible ones and log which was used. A silent failure here degrades to a
# bare 404 with nothing to explain it, which is a miserable thing to debug on
# a host you cannot shell into.

STATIC_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "static",   # container image
    Path.cwd() / "static",                               # cwd-relative
    Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",  # checkout
]

STATIC_DIR = next((p for p in STATIC_CANDIDATES
                   if (p / "index.html").is_file()), None)

if STATIC_DIR is not None:
    log.info("serving frontend from %s", STATIC_DIR)

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """Return index.html for any non-API path.

        The client is a single-page app: it owns its own routing, so a deep
        link has to reach index.html rather than 404. Real files are served
        directly when they exist so a favicon or manifest still resolves.
        """
        root = STATIC_DIR.resolve()
        candidate = (root / full_path).resolve()
        # Resolve and compare so a crafted path cannot escape the directory.
        if candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
else:
    log.warning("no frontend build found, serving the API only. Looked in: %s",
                ", ".join(str(p) for p in STATIC_CANDIDATES))
