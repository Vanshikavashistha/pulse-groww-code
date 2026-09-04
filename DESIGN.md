# Pulse — design notes

The brief asks for a watchlist that tells a user what has *meaningfully
changed since they last checked*. Almost every part of that sentence is a
design decision in disguise, and this document is where I make each of them
explicit, including the ones I got wrong first.

---

## 1. The thing I decided the product actually is

A live price table is not a hard problem, and it does not answer the brief.
The brief's question is closer to the one an inbox answers than the one a
ticker answers: *is there anything here I need to deal with?*

So Pulse is built as an inbox for a portfolio. Two consequences follow from
taking that seriously:

- **The default state is empty.** If nothing unusual happened, the feed says
  so and shows nothing. A watchlist that always has something to say has
  taught the user to ignore it.
- **Read state is a first-class concept.** "Since you last checked" is
  meaningless without storing where the user stopped reading. That storage is
  the core of the system, not a feature bolted onto a price table.

**What I rejected:** an alerts-with-thresholds product, where the user
configures "tell me if RELIANCE drops 3%". It puts the analytical work back
on the user, most people never configure it, and it cannot answer "what
deserves attention now" for a symbol the user never thought to set a rule on.

---

## 2. What counts as a meaningful change

This is the judgement the whole product rests on.

**The obvious rule is wrong.** A flat threshold — flag anything past ±2% —
fails in both directions simultaneously. ADANIENT moves 2% on an ordinary
afternoon; MARUTI moving 2% is genuinely news. A flat rule floods the user
with noise on volatile names and stays silent on precisely the large-cap
moves they would most want to know about.

**So significance is measured in units of the symbol's own volatility.**

```
z = return / sigma_symbol
```

`sigma` is an EWMA of squared returns with λ = 0.94 (the RiskMetrics
standard). EWMA rather than a simple rolling standard deviation because
volatility clusters: after a shock, the next move genuinely is more likely to
be large, and an exponentially-weighted estimate adapts within a few ticks
instead of lagging an entire window.

The default alert threshold is 2σ — roughly the most unusual 5% of moves for
that symbol, which for a list of ten stocks keeps the feed to a handful of
items a day rather than a constant trickle.

Two guards keep the maths from embarrassing itself:

| Failure mode | Guard |
|---|---|
| A stock that has been perfectly flat drives σ → 0, so every rounding tick becomes a ten-sigma event | `MIN_ABS_MOVE_PCT` floor — never interrupt anyone over 0.1% |
| A newly added symbol has no history, so any z-score is fabricated confidence | Below 12 samples, fall back to a flat 2% rule **and tell the user that is what happened** |

That second one matters more than it looks. The UI shows the reason a signal
fired ("3.1x this symbol's typical move" versus "flat 2% rule, still learning
this symbol"). A user who can see *why* something was flagged can calibrate
their trust in the flagging. A black box that is right 80% of the time is
less useful than a transparent one that is right 75%.

### Matching the horizon (the bug that nearly shipped)

The first working version reported moves of thirty-eight sigma. That figure
is not a dramatic market; it is a unit error, and it is worth writing down
because the fix is the most interesting piece of maths in the system.

`sigma` is estimated from *tick-to-tick* returns. But the move a user cares
about is cumulative since the previous close, spanning hundreds of ticks.
Dividing a whole day's move by a single tick's volatility inflates the score
by roughly the square root of the number of ticks. Any reading above about
five sigma in a system like this is not a discovery, it is a signal that the
two sides of the division are measured over different periods.

Under the random-walk assumption, variance accumulates linearly with time, so
volatility grows with its square root:

```
sigma_n = sigma_tick * sqrt(n)
```

This is the same square-root-of-time rule used to annualise volatility on any
risk desk. The assumption is imperfect — real returns are fat-tailed and
mildly autocorrelated, so this slightly understates tail risk — but it is the
correct first-order fix, and it is what turns the readings from nonsense back
into numbers a person can reason about.

The horizon is capped at one session (`HORIZON_CAP_TICKS`). Without a cap, a
process left running for days keeps widening its own definition of normal
until nothing is ever unusual again — a system that slowly goes quiet is more
dangerous than one that is noisy, because the silence looks like calm.

`test_cumulative_move_is_judged_against_matched_horizon_volatility` pins both
halves: the unscaled figure is absurd, the scaled one is ordinary, and the
ratio is exactly sqrt(n).

**Beyond price**, three more detectors run, because "meaningful" is not only
magnitude:

- **Trend reversal** — crossing back through the previous close. Weighted
  *higher* than an equivalent-magnitude continuation, because a stock
  changing direction changes a decision more often than one continuing in it.
- **Volume surge** — above 2.5x the symbol's own rolling average. Volume is
  confirmation: a 2σ move on ordinary volume is often noise, the same move on
  4x volume usually is not.
- **Range break** — a new session extreme, but only if it clears the old one
  by a real margin. The first version fired on any new high, which in a
  trending series is almost every tick; it buried the signals that mattered
  under the ones that did not. Requiring a margin is the difference between a
  detector and a ticker.
- **Data staleness** — see §5.

---

## 3. "Since *you* last checked" — the architecture

There are two genuinely different kinds of change, and conflating them is why
most watchlists cannot answer the brief's question.

**Discrete events are shared.** A volume surge in ZOMATO is the same fact for
every user watching it. It is detected once by a single background poller and
appended to an immutable `signals` log. Reading it is a cursor comparison:
`signal.id > my last_seen_signal_id`.

**Continuous drift is personal.** The same stock at the same price is a
different message depending on when you last looked. Down 0.4% since ten
minutes ago is nothing. Down 0.4% since Monday means three flat days, which
is itself information. This is measured from `last_seen_price` — the price
that user actually had on screen — not from the previous close, which is what
every other watchlist shows.

The feed is the union of both, ranked by attention score.

```
  poller (single writer)          per-user read
  ──────────────────────          ─────────────
  fetch batch  ──► ingest ──► signals (append-only, monotonic id)
                      │                    │
                      └──► symbols         │  id > cursor
                           (last price,    ▼
                            EWMA σ)   watchlist_items
                                      (last_seen_price,
                                       last_seen_at,
                                       last_seen_signal_id)
```

### Fan-in on read, not fan-out on write

The alternative design writes a copy of every signal into every subscriber's
personal inbox at detection time. Reads become trivial; writes multiply by
the number of users watching that symbol — which, for RELIANCE on a broking
platform, is close to the entire user base. One market event becomes millions
of rows.

I chose fan-in: store the signal once, join to users at read time. The read
is a range scan on `ix_signals_ticker_id`, so it is cheap, and write cost
stays `O(distinct symbols)` regardless of user count.

**Where this flips:** fan-out wins when reads vastly outnumber writes *and*
the fan-out factor is small. Neither holds here — signals are rare (a handful
per symbol per day) and the fan-out factor is enormous. If Pulse ever needed
to push notifications rather than serve a pulled feed, the calculus changes,
and the right move would be a hybrid: fan-out for the small set of users with
push enabled, fan-in for everyone else. That is the standard resolution to
the celebrity-follower problem and it applies cleanly here.

---

## 4. The race I designed around

The subtle bug in any read-state system:

1. The client renders a feed ending at signal id 41.
2. Signal 42 is detected while the user is reading.
3. The user presses "Mark all as seen".

If the server handles this as `UPDATE cursor = (SELECT MAX(id) FROM signals)`,
signal 42 is consumed without ever being displayed. The user is never told,
and cannot be told, because the evidence is gone. In a financial product
that is a silent correctness failure — the worst kind.

**The fix:** the client sends back the cursor it *actually rendered*. The
server never invents one. Anything detected during the round trip survives
to the next visit.

The cursor is also monotonic — `mark_seen` only ever moves it forward — so a
duplicated or out-of-order acknowledgement is a no-op rather than a
regression. Both behaviours are pinned by tests
(`test_signal_arriving_during_acknowledgement_stays_unread`,
`test_cursor_only_moves_forward`).

This is also why signals are never deleted or mutated on read. Marking as
seen touches a per-user integer, not the shared log, so reads and writes
never contend for the same rows.

### A second race, found by running it

The client loads the feed and the watchlist in parallel. For a handle seen
for the first time, both requests find no user, both insert, and one dies on
the unique constraint — a 500 on the very first page load, which is the worst
possible moment for one.

The fix follows the same principle as everything else here: **let the database
arbitrate, not the application**. The loser of the insert rolls back and
re-reads the row the winner just committed. Checking "does this user exist"
before inserting cannot work, because the gap between the check and the
insert is exactly where the race lives. Only the unique constraint knows the
truth, so the constraint is what decides.

---

## 5. Stale, delayed and conflicting data

Free market data is unreliable in specific, predictable ways, so the system
treats trust as a value it carries rather than an assumption it makes.

**Every quote carries its provenance:** exchange timestamp, receipt time,
source, and whether the vendor declares it delayed.

**Ingest enforces monotonic time.** A quote is applied only if its `as_of` is
newer than the stored one. Providers retry, replay and reorder; without this
guard a late tick rewinds the price and — worse — generates a phantom signal
for a move that never happened. Tested in
`test_out_of_order_tick_is_rejected`.

**Staleness is surfaced, not hidden.** Past 120 seconds without a fresh quote,
the price is labelled in the table *and* a `STALE_DATA` signal enters the
feed. This is a deliberate product position: in a financial interface, an
unlabelled stale price is worse than a missing one, because the user cannot
distinguish a stock that has not moved from a feed that has stopped — and
only one of those should change their behaviour. Showing our own failure
costs a little polish and buys the only thing that matters here, which is
that the user can trust what they see.

**A circuit breaker** sits in front of the provider: four consecutive
failures and we stop calling for 60 seconds, serving last-known-good prices
with the connection state shown honestly in the header. Continuing to hammer
a struggling upstream slows its recovery and burns the rate limit the healthy
path needs. It reopens half-open — one probe request, not a thundering herd.

**Duplicate suppression is a database guarantee.** Each signal has a
fingerprint of `(ticker, type, quantised magnitude band)` under a unique
index. The bands are deliberately coarse: fingerprinting on the exact
percentage meant a stock grinding from 3% to 4% produced a fresh alert at
every step, which is precisely the flooding this product exists to prevent.
Banding means the user hears "this is moving" once, and hears again only when
it has moved somewhere materially different. A stock that stays down 4σ would otherwise re-alert on every poll;
quantisation collapses one continuing condition into one signal per cooldown
window. Putting it in the index rather than in application logic means it
still holds if a second poller is ever added.

---

## 6. Scaling

Current shape: one poller thread, batched fetches, one row per symbol.
Comfortably handles thousands of symbols and, because signal storage is
shared, a large user count on modest hardware.

The honest bottlenecks, in the order they would bite:

1. **Poll frequency versus vendor rate limits.** Fix: tiered polling. Symbols
   on many watchlists or currently moving get 5-second polls; quiet ones drop
   to 60. Attention is not uniform and neither should refresh be.
2. **Single writer thread.** Fix: partition symbols by consistent hash across
   N workers. The design already permits this — signal writes are idempotent
   under the fingerprint index, so workers cannot double-emit.
3. **SQLite write throughput.** WAL mode gets us far, but Postgres is the
   move at real scale. The SQLAlchemy layer makes it a URL change; nothing in
   the application assumes SQLite semantics.
4. **Feed assembly per request.** Fix: cache the shared portion of a feed per
   symbol set and apply the per-user cursor over it, since the expensive part
   (signal lookup) is identical for users watching the same symbols.

---

## 7. The first two minutes

A design decision that is not about the market at all.

Volatility estimates need history. Straight after startup there is none, so
the honest output is "still learning" for every symbol and an empty feed —
and that is exactly the screen someone evaluating this project would spend
their first two minutes looking at. A correct system that appears to do
nothing is, for that reviewer, indistinguishable from one that does nothing.

So the simulated provider replays a session before the app serves its first
request: a long quiet phase that only builds statistics, then a shorter phase
that also emits signals, stamped across the preceding couple of hours.

Two parameters had to be balanced against each other, and both failed loudly
before they worked:

- **Too few emitting cycles** and the whole phase falls inside one cooldown
  window, so the fingerprint collapses it to a single signal — the
  deduplication working exactly as designed, against me.
- **Too many price steps** and the random walk wanders to eleven percent off
  the open. A large cap showing that undermines the credibility of every
  other number on the screen.

The resolution was to advance the synthetic clock faster than a real poll:
the replay covers a couple of hours in a couple of hundred price steps, which
spans several cooldown windows without letting the walk drift somewhere no
real stock would go.

It runs for `replay` only, and the README says plainly that it happens. A
warmed-up demo is a fair presentation of a working system; one that hid what
it was doing would not be.

## 8. Things I deliberately did not build

Listing these because a 72-hour build is defined as much by what it leaves
out, and unmarked absences read as oversights.

- **Authentication.** A handle in a header. Real auth demonstrates nothing
  the brief asks about. The data model *is* genuinely multi-tenant — change
  the handle in the header and you get a different reading position, which is
  the part that mattered.
- **Charts.** They answer "what has this stock been doing", which is a
  question every existing app already answers well. This product answers a
  different one.
- **News and sentiment.** Tempting, and the obvious way to make "meaningful"
  sound smart. But a headline classifier built in 72 hours is a plausible-
  sounding random number generator, and shipping one into a financial
  interface would be worse than not having it. The volume detector is the
  honest, verifiable proxy for "something happened that price alone does not
  explain".
- **Mobile push.** Changes the fan-out calculus (see §3) and is a delivery
  problem rather than a detection one.

---

## 9. What I would do next

- Replace the session-range break with a true 52-week range from historical
  data; the current window is bounded by process lifetime.
- Cluster correlated signals. If eight banking stocks all move 2σ together,
  that is one sector event, not eight alerts. Correlation-aware grouping is
  the single largest remaining reduction in feed noise.
- Learn per-user thresholds from what they engage with, so the definition of
  "meaningful" adapts to the person rather than staying global.
- Property-based testing over the ingest path with generated adversarial tick
  sequences, rather than the specific cases I hand-wrote.
