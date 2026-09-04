import React, { useState } from 'react'
import Sparkline from './Sparkline'

function money(value) {
  if (value == null) return '—'
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export default function Watchlist({ items, universe, onAdd, onRemove, onPin, error }) {
  const [draft, setDraft] = useState('')

  const submit = async () => {
    const ticker = draft.trim().toUpperCase()
    if (!ticker) return
    await onAdd(ticker)
    setDraft('')
  }

  const held = new Set(items.map((item) => item.ticker))
  const unheld = universe.filter((entry) => !held.has(entry.ticker)).slice(0, 6)

  return (
    <section>
      <div className="column-head">
        <h2>Watchlist</h2>
        <span className="count">{items.length} symbols</span>
      </div>

      {items.length === 0 ? (
        <p className="notice">
          <strong>Add a symbol to begin.</strong> Once something is on this list we
          start learning how it normally moves, so we can tell you when it does
          something it usually does not.
        </p>
      ) : (
        <ul className="holdings">
          {items.map((item) => {
            const direction = (item.change_pct ?? 0) >= 0 ? 'up' : 'down'
            return (
              <li className="holding" key={item.ticker} data-stale={item.is_stale}>
                <button
                  className="pin"
                  data-on={item.pinned}
                  onClick={() => onPin(item.ticker, !item.pinned)}
                  aria-label={item.pinned ? `Unpin ${item.ticker}` : `Pin ${item.ticker}`}
                  title="Pinned symbols rank higher in your feed"
                >
                  {item.pinned ? '★' : '☆'}
                </button>

                <div className="holding-id">
                  <span className="holding-ticker">{item.ticker}</span>
                  <span className="holding-name">{item.name}</span>
                  {item.drift_pct != null && Math.abs(item.drift_pct) >= 0.4 && (
                    <span className="holding-drift num" data-direction={item.drift_pct >= 0 ? 'up' : 'down'}>
                      {item.drift_pct >= 0 ? '+' : ''}{item.drift_pct.toFixed(2)}% since you looked
                    </span>
                  )}
                </div>

                <Sparkline points={item.spark} direction={direction} />

                <div className="holding-figures">
                  <span className="holding-price num">{money(item.price)}</span>
                  <span className={`holding-change num ${direction}`}>
                    {item.change_pct == null
                      ? '—'
                      : `${item.change_pct >= 0 ? '+' : ''}${item.change_pct.toFixed(2)}%`}
                  </span>
                </div>

                {item.is_stale && (
                  <span className="stale-flag">
                    {item.age_seconds != null ? `${Math.floor(item.age_seconds / 60)}m old` : 'stale'}
                  </span>
                )}

                <button className="link-action" onClick={() => onRemove(item.ticker)}>
                  Remove
                </button>
              </li>
            )
          })}
        </ul>
      )}

      <div className="add-row">
        <input
          className="add-input"
          value={draft}
          placeholder="Add a symbol, e.g. TCS"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && submit()}
          aria-label="Add a symbol to your watchlist"
        />
        <button className="action" onClick={submit} disabled={!draft.trim()}>Add</button>
      </div>

      {unheld.length > 0 && (
        <div className="suggestions">
          {unheld.map((entry) => (
            <button key={entry.ticker} className="suggestion"
                    onClick={() => onAdd(entry.ticker)} title={entry.name}>
              {entry.ticker}
            </button>
          ))}
        </div>
      )}

      {error && <p className="error-bar">{error}</p>}
    </section>
  )
}
