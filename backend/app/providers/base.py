"""Market data provider contract.

Data sources for Indian equities are unreliable in specific, predictable ways:
they rate-limit, they go down, they return yesterday's close during a halt,
and they occasionally return ticks out of order. Rather than spread those
concerns through the app, every provider returns the same `Quote` shape and
every quote carries its own provenance -- when the exchange stamped it, when
we received it, who told us, and whether it is delayed.

Downstream code is then able to reason about trust instead of assuming it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    prev_close: float | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    volume: float | None
    as_of: datetime          # exchange time for this price
    source: str
    is_delayed: bool = False
    name: str = ""


class ProviderError(Exception):
    """Upstream failed. Callers degrade; they do not crash."""


class MarketDataProvider(Protocol):
    name: str

    def fetch(self, tickers: Sequence[str]) -> list[Quote]:
        """Return quotes for as many tickers as possible.

        Partial success is normal and must not be treated as failure: if nine
        of ten symbols resolve, the user should see nine fresh prices and one
        marked stale. Raise ProviderError only when nothing could be fetched.
        """
        ...
