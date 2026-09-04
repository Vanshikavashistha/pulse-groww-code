from pydantic import BaseModel, Field


class AddSymbolRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)


class MarkSeenRequest(BaseModel):
    # The highest signal id the client actually rendered. See feed.mark_seen
    # for why the server does not just use its own MAX(id).
    ack_cursor: int = 0
    tickers: list[str] | None = None


class PinRequest(BaseModel):
    pinned: bool
