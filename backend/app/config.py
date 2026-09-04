"""Central configuration.

Every number that encodes a *judgement call* lives here rather than being
scattered through the code, so the thresholds that define "meaningful" are
auditable in one place. That matters: this file is effectively the product
specification for what deserves a user's attention.
"""
import os


class Settings:
    # --- Storage -----------------------------------------------------------
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./pulse.db")

    # --- Market data -------------------------------------------------------
    # "yahoo"  -> live NSE quotes via Yahoo Finance (no API key needed)
    # "replay" -> deterministic simulator; used when the exchange is closed
    #             so the product can be demoed at any hour.
    PROVIDER: str = os.getenv("PROVIDER", "replay")
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
    PROVIDER_TIMEOUT_SECONDS: float = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "6"))

    # Warmup: cycles of simulated history replayed at startup so the first
    # screen has real statistics behind it. Only applies to the replay
    # provider. See Poller.warmup.
    WARMUP_CYCLES: int = 330
    WARMUP_QUIET_CYCLES: int = 180   # of those, how many build stats silently
    # The synthetic clock advances faster than a real poll, so the replayed
    # session covers a couple of hours in a couple of hundred price steps.
    # Both ends matter: the emitting phase has to span several cooldown
    # windows or the fingerprint collapses it to one signal, and the number of
    # price steps has to stay small or the random walk wanders somewhere no
    # real stock would go and undermines everything else on the screen.
    WARMUP_CLOCK_STEP_SECONDS: int = 30

    # --- Reliability -------------------------------------------------------
    # Circuit breaker: after N consecutive upstream failures we stop calling
    # the provider for a cooldown window and serve last-known-good prices,
    # clearly labelled as stale.
    BREAKER_FAILURE_THRESHOLD: int = 4
    BREAKER_COOLDOWN_SECONDS: int = 60
    # A quote older than this is surfaced to the user as stale.
    STALE_AFTER_SECONDS: int = 120

    # --- Significance model ------------------------------------------------
    # A move is significant when it is large *relative to how that stock
    # normally moves*, not when it crosses a flat percentage. 2% in HDFCBANK
    # is news; 2% in a small-cap is Tuesday.
    Z_SCORE_THRESHOLD: float = 2.0        # sigma multiples before we alert
    MIN_ABS_MOVE_PCT: float = 0.4         # floor, stops noise on dead-flat stocks
    VOLUME_SURGE_RATIO: float = 2.5       # vs. the symbol's own rolling average
    RANGE_WINDOW_TICKS: int = 300         # ticks retained for the range/high-low test
    # A new extreme only counts if it clears the old one by a real margin. In
    # a trending series the running high is beaten almost every tick, and
    # reporting each one would bury the signals that actually matter.
    RANGE_BREAK_MARGIN_PCT: float = 0.6
    VOLATILITY_EWMA_LAMBDA: float = 0.94  # RiskMetrics-standard decay factor
    VOLATILITY_MIN_SAMPLES: int = 12      # below this we fall back to the flat rule
    # Volatility is scaled to the horizon of the move being judged, capped at
    # one session. Without a cap, a process left running for days keeps
    # widening its own definition of normal until nothing is ever unusual.
    HORIZON_CAP_TICKS: int = 240

    # Per (symbol, signal_type) silence window. Without this a 3-sigma move
    # would re-fire on every single poll for as long as it persists.
    SIGNAL_COOLDOWN_SECONDS: int = 600

    # Feed hygiene
    FEED_MAX_ITEMS: int = 60
    SIGNAL_RETENTION_HOURS: int = 72

    # --- Attention scoring weights ----------------------------------------
    W_MAGNITUDE: float = 1.0    # how unusual the move is (|z|)
    W_VOLUME: float = 0.5       # confirmation from volume
    W_TYPE: float = 1.0         # inherent severity of the event type
    W_PIN: float = 1.5          # user explicitly pinned this symbol
    RECENCY_HALFLIFE_MINUTES: float = 90.0


settings = Settings()
