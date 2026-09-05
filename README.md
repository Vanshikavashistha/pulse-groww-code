# Pulse

A market watchlist that answers one question: **what changed since you last
looked?**

Most watchlists show you a price. Pulse tracks where your attention stopped
last time and shows you only what moved unusually since then — measured
against each symbol's own volatility, not a flat percentage. If nothing
unusual happened, it tells you that and shows you nothing.

The reasoning behind every significant choice is in **[DESIGN.md](DESIGN.md)**.

---

## Run it

Two terminals. Takes about a minute.

### Backend

**Python 3.11 or 3.12.** On 3.13+ some dependencies have no prebuilt wheel yet
and pip falls back to compiling from source, which fails without a Rust
toolchain. If `pip install` dies on `pydantic-core`, that is what happened —
create the environment with an older interpreter:

```bash
python3.12 -m venv .venv        # macOS: brew install python@3.12
```

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Startup takes a few seconds — see the warmup note below.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**.

### With Docker instead

One image, one service: the API and the built frontend on the same origin.

```bash
docker build -t pulse . && docker run -p 8000:8000 pulse
```

Then open **http://localhost:8000** — no separate frontend server.

### Deploying it

`render.yaml` deploys the Dockerfile as a single web service. Point Render at
the repository and it picks the config up; the health check is `/api/health`.

**On a free instance, expect a slow first load.** The container sleeps after
inactivity, so the first request pays a cold start of roughly a minute before
the app's own startup warmup begins. That is the hosting tier, not the
application — running it locally with the commands above starts in seconds.

---

## Which market data source

Set with the `PROVIDER` environment variable.

| Value | What it does |
|---|---|
| `replay` *(default)* | A simulated market. Runs at any hour. |
| `yahoo` | Live NSE quotes via Yahoo Finance. No API key needed. |

```bash
PROVIDER=yahoo uvicorn app.main:app --port 8000
```

**Why `replay` is the default, and why it is not a stub.** NSE trades
09:15–15:30 IST on weekdays. Demoed at 11pm on a Saturday, a live-only build
shows a frozen screen, which proves nothing about whether the change
detection works. The replay provider gives each symbol its own volatility —
which is precisely the situation the significance engine exists to handle, a
move that is unremarkable for one stock and a three-sigma event for another
— and it injects the failure modes worth designing for: dropped ticks,
duplicated payloads, and out-of-order timestamps.

Switch to `yahoo` to see it against the real market during trading hours.

**On startup the replay provider replays a session before serving anything.**
The volatility estimates need history before they mean anything, so without
this the first two minutes show an empty feed and a band reading "learning"
for every symbol — which is the one screen that demonstrates nothing. The
warmup runs a couple of hundred price steps against a synthetic clock: a long
quiet phase that only builds the statistics, then a shorter phase that also
emits signals, timestamped across the preceding couple of hours. So the first
screen reads like a morning you missed rather than a burst of events that all
happened at once.

This is the simulator initialising itself, not fabricated data, and it runs
only for `replay` — live market data has its own history and needs no help.

---

## Try these

The behaviour worth looking at is not on the first screen.

1. **The feed is already populated when you open it** — that is the replayed
   session described above. Each entry states the rule that fired it. Leave
   the tab open and new ones arrive live.
2. **Press "Mark all as seen", then wait.** The feed empties, and refills only
   with what happened *after* that moment. That is the whole product.
3. **Change the handle in the top-right** to any other word. Different user,
   different reading position, same market — because read state is per user,
   not global.
4. **Add a fresh symbol** (try `PAYTM` or `ADANIENT`). Its first few signals
   say "still learning this symbol's volatility", because with under 12
   samples the engine falls back to a flat rule and says so instead of
   inventing confidence.
5. **Stop the backend for two minutes, then restart it.** The header turns
   red, prices are labelled with their age rather than shown as current, and
   a data-gap entry appears in the feed. See DESIGN.md §5 for why surfacing
   this was a deliberate choice.

---

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

Twelve tests, covering the four places this system could mislead someone
about money: mis-scoring a move, replaying a stale tick, alerting twice for
one event, and losing an event to the race between rendering a feed and
acknowledging it.

---

## Layout

```
backend/
  app/
    config.py               every threshold that encodes a judgement call
    models.py               schema; the two-layer read-state design
    providers/
      base.py               provider contract; quotes carry their provenance
      yahoo.py              live NSE quotes, batched
      replay.py             simulated market with injected failure modes
    engine/
      significance.py       what counts as meaningful (pure, unit-tested)
      detector.py           quotes to signals; ordering and dedup invariants
      feed.py               per-user assembly; the mark-as-seen race
      poller.py             single writer, circuit breaker
    main.py                 REST + SSE
  tests/test_engine.py
frontend/
  src/
    App.jsx                 state, streaming, actions
    api.js                  client, including the SSE subscription
    components/Feed.jsx     the inbox
    components/Watchlist.jsx
DESIGN.md                   the reasoning
```

---

## Stack, and why

**FastAPI + SQLAlchemy + SQLite.** Synchronous on purpose: the write path is
a single background poller and reads are short indexed lookups, so an async
driver would add failure modes without buying throughput at this scale.
Postgres is a connection-string change.

**React + Vite**, no state library. The app has one refresh path and roughly
six pieces of state; a store would be ceremony.

**Server-sent events, not WebSockets.** The data flow is strictly server to
client. SSE gives automatic browser reconnection and ordinary HTTP semantics,
with no second protocol to debug. The stream carries a version marker rather
than the payload, so there is exactly one code path that assembles a feed and
a client that missed a push converges on the next poll instead of holding a
permanently wrong view.
