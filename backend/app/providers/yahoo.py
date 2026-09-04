"""Live NSE quotes via Yahoo Finance.

Chosen because it needs no API key, covers NSE via the `.NS` suffix, and
supports batched requests -- one HTTP call serves every symbol on the platform
rather than one call per user per symbol. That batching is what keeps the
system inside free-tier rate limits as the user count grows.

The `.NS` suffix is applied here and nowhere else, so the rest of the system
speaks plain tickers (RELIANCE) and stays portable to another vendor.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

import httpx

from .base import MarketDataProvider, ProviderError, Quote

log = logging.getLogger(__name__)

ENDPOINT = "https://query1.finance.yahoo.com/v7/finance/quote"
BATCH_SIZE = 40  # vendor truncates very long symbol lists
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PulseWatchlist/1.0)"}


class YahooProvider(MarketDataProvider):
    name = "yahoo"

    def __init__(self, timeout: float = 6.0, suffix: str = ".NS"):
        self._timeout = timeout
        self._suffix = suffix

    def _vendor_symbol(self, ticker: str) -> str:
        return ticker if "." in ticker else f"{ticker}{self._suffix}"

    def _plain_ticker(self, vendor_symbol: str) -> str:
        return vendor_symbol.replace(self._suffix, "")

    def fetch(self, tickers: Sequence[str]) -> list[Quote]:
        if not tickers:
            return []

        quotes: list[Quote] = []
        failures = 0
        batches = [list(tickers)[i:i + BATCH_SIZE]
                   for i in range(0, len(tickers), BATCH_SIZE)]

        with httpx.Client(timeout=self._timeout, headers=HEADERS) as client:
            for batch in batches:
                params = {"symbols": ",".join(self._vendor_symbol(t) for t in batch)}
                try:
                    response = client.get(ENDPOINT, params=params)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    failures += 1
                    log.warning("yahoo batch failed: %s", exc)
                    continue

                for row in payload.get("quoteResponse", {}).get("result", []):
                    quote = self._parse(row)
                    if quote is not None:
                        quotes.append(quote)

        # Total failure is an outage; partial failure is ordinary life.
        if not quotes and failures:
            raise ProviderError("yahoo returned no usable quotes")
        return quotes

    def _parse(self, row: dict) -> Quote | None:
        price = row.get("regularMarketPrice")
        symbol = row.get("symbol")
        if price is None or symbol is None:
            return None

        epoch = row.get("regularMarketTime")
        as_of = (datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
                 if epoch else datetime.utcnow())

        # Yahoo advertises its own delay; we pass that honesty through to the UI
        # rather than presenting delayed data as live.
        delay_minutes = row.get("exchangeDataDelayedBy") or 0

        return Quote(
            ticker=self._plain_ticker(symbol),
            price=float(price),
            prev_close=_as_float(row.get("regularMarketPreviousClose")),
            day_open=_as_float(row.get("regularMarketOpen")),
            day_high=_as_float(row.get("regularMarketDayHigh")),
            day_low=_as_float(row.get("regularMarketDayLow")),
            volume=_as_float(row.get("regularMarketVolume")),
            as_of=as_of,
            source=self.name,
            is_delayed=delay_minutes > 0,
            name=row.get("shortName") or row.get("longName") or "",
        )


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
