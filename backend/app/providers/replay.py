"""A simulated market, used when the real one is shut.

NSE trades 09:15-15:30 IST on weekdays. A watchlist demoed at 11pm on a
Saturday would otherwise show a frozen screen, which tells a reviewer nothing
about whether the change-detection actually works. This provider generates a
plausible market so the product can be evaluated at any hour, and so the
significance engine can be exercised against reproducible scenarios.

It is not a toy stub. Each symbol has its own volatility, which is the point:
it produces exactly the situation the significance model exists to handle --
a 1.5% move that is unremarkable for one stock and a three-sigma event for
another. It also injects the failure modes worth designing for: occasional
gaps, a duplicated tick, and a deliberately out-of-order timestamp.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Sequence

from .base import MarketDataProvider, Quote

# ticker -> (display name, opening price, per-tick volatility, typical volume)
#
# Per-tick volatility is calibrated so that scaling it over a session
# (sqrt(240) is roughly 15.5) lands near each name's real daily volatility:
# about 1.2% for a steady large cap, 3.5% for a volatile small cap. Getting
# this right matters more than it looks -- it is what makes the demo show a
# genuine spread of significance rather than every symbol looking equally
# dramatic.
UNIVERSE: dict[str, tuple[str, float, float, float]] = {
    "RELIANCE":   ("Reliance Industries",        2870.0, 0.00085, 5_200_000),
    "HDFCBANK":   ("HDFC Bank",                  1685.0, 0.00065, 8_100_000),
    "TCS":        ("Tata Consultancy Services",  3910.0, 0.00075, 2_300_000),
    "INFY":       ("Infosys",                    1620.0, 0.00095, 6_800_000),
    "TATAMOTORS": ("Tata Motors",                 985.0, 0.00165, 14_500_000),
    "ZOMATO":     ("Zomato",                      268.0, 0.0023, 31_000_000),
    "ITC":        ("ITC",                         438.0, 0.0005, 9_400_000),
    "SBIN":       ("State Bank of India",         842.0, 0.00115, 12_700_000),
    "PAYTM":      ("One 97 Communications",       795.0, 0.0028, 4_900_000),
    "ADANIENT":   ("Adani Enterprises",          2410.0, 0.0031, 3_300_000),
    "BAJFINANCE": ("Bajaj Finance",              7180.0, 0.00105, 1_100_000),
    "MARUTI":     ("Maruti Suzuki",             12450.0, 0.00078, 620_000),
}


class ReplayProvider(MarketDataProvider):
    name = "replay"

    def __init__(self, seed: int | None = None, chaos: bool = True):
        self._rng = random.Random(seed)
        self._chaos = chaos
        self._state: dict[str, dict] = {}
        self._ticks = 0

    def _init_symbol(self, ticker: str) -> dict:
        name, open_price, vol, volume = UNIVERSE.get(
            ticker, (ticker, 1000.0, 0.003, 1_000_000))
        prev_close = round(open_price * (1 + self._rng.gauss(0, 0.004)), 2)
        return {
            "name": name, "price": open_price, "vol": vol,
            "base_volume": volume, "prev_close": prev_close,
            "open": open_price, "high": open_price, "low": open_price,
            "cum_volume": volume * 0.1,
            "drift": self._rng.gauss(0, 0.00008),
            "last_as_of": datetime.utcnow(),
        }

    def _step(self, state: dict) -> None:
        """One geometric-Brownian-ish step, with occasional regime shocks."""
        shock = 0.0
        if self._rng.random() < 0.012:                      # news-like jump
            shock = self._rng.choice([-1, 1]) * state["vol"] * self._rng.uniform(12, 30)
        # Mean reversion toward the open. Without it a random walk left
        # running for an hour wanders to an implausible place, and a demo
        # showing a large cap up eleven percent undermines the credibility of
        # everything else on the screen.
        pull = -0.004 * (state["price"] / state["open"] - 1.0)
        move = self._rng.gauss(state["drift"] + pull, state["vol"]) + shock

        state["price"] = max(1.0, round(state["price"] * (1 + move), 2))
        state["high"] = max(state["high"], state["price"])
        state["low"] = min(state["low"], state["price"])

        surge = 6.0 if abs(shock) > 0 else self._rng.uniform(0.6, 1.5)
        state["cum_volume"] += state["base_volume"] * 0.02 * surge

    def fetch(self, tickers: Sequence[str]) -> list[Quote]:
        self._ticks += 1
        now = datetime.utcnow()
        quotes: list[Quote] = []

        for ticker in tickers:
            state = self._state.setdefault(ticker, self._init_symbol(ticker))

            # Realistic gap: a symbol simply does not report on this poll.
            if self._chaos and self._rng.random() < 0.04:
                continue

            self._step(state)
            as_of = now

            # Out-of-order tick, roughly once every fifty polls. The ingest
            # path is expected to reject it rather than rewind the price.
            if self._chaos and self._rng.random() < 0.02:
                as_of = state["last_as_of"] - timedelta(seconds=30)
            else:
                state["last_as_of"] = as_of

            quotes.append(Quote(
                ticker=ticker,
                price=state["price"],
                prev_close=state["prev_close"],
                day_open=state["open"],
                day_high=state["high"],
                day_low=state["low"],
                volume=round(state["cum_volume"]),
                as_of=as_of,
                source=self.name,
                is_delayed=False,
                name=state["name"],
            ))

        # Duplicate tick: the same payload arrives twice. Ingest must be
        # idempotent, and signal generation must not double-fire.
        if self._chaos and quotes and self._rng.random() < 0.05:
            quotes.append(quotes[0])

        return quotes
