import React from 'react'

const TYPE_LABEL = {
  PRICE_MOVE: 'unusual move',
  TREND_REVERSAL: 'reversed',
  VOLUME_SURGE: 'volume',
  RANGE_BREAK: 'range break',
  STALE_DATA: 'data gap',
  PERSONAL_DRIFT: 'since your visit',
}

function relativeTime(iso) {
  if (!iso) return ''
  const seconds = Math.max(0, (Date.now() - new Date(iso + 'Z').getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function lastCheckedPhrase(iso) {
  if (!iso) return 'This is your first visit'
  const minutes = (Date.now() - new Date(iso + 'Z').getTime()) / 60000
  if (minutes < 2) return 'You checked a moment ago'
  if (minutes < 60) return `You last checked ${Math.round(minutes)} minutes ago`
  if (minutes < 1440) return `You last checked ${Math.round(minutes / 60)} hours ago`
  return `You last checked ${Math.round(minutes / 1440)} days ago`
}

/* A meter, not a badge.
 *
 * Showing "3.1σ" as text tells you the number. Showing it as a filled bar
 * against the threshold tells you what the number means, which is the part a
 * user actually needs: was this barely over the line, or nowhere near it.
 */
function SigmaMeter({ magnitude, severity }) {
  if (!magnitude) return null
  const fill = Math.min(magnitude / 4.5, 1) * 100
  return (
    <div className="meter" title={`${magnitude.toFixed(1)} sigmas from this symbol's average move`}>
      <div className="meter-track">
        <div className="meter-fill" data-severity={severity} style={{ width: `${fill}%` }} />
        <div className="meter-mark" style={{ left: `${(2 / 4.5) * 100}%` }} />
      </div>
      <span className="meter-value num">{magnitude.toFixed(1)}σ</span>
    </div>
  )
}

export default function Feed({ feed, loading, onMarkSeen, marking }) {
  if (loading && !feed) {
    return (
      <section>
        <div className="column-head"><h2>What changed</h2></div>
        <p className="notice">Reading the market.</p>
      </section>
    )
  }

  const entries = feed?.entries ?? []
  const settled = entries.length === 0

  return (
    <section>
      <div className="column-head">
        <h2>
          What changed
          {entries.length > 0 && <span className="pill">{entries.length}</span>}
        </h2>
        {entries.length > 0 && (
          <button className="action action-primary" onClick={onMarkSeen} disabled={marking}>
            {marking ? 'Marking' : 'Mark all as seen'}
          </button>
        )}
      </div>

      {/* The reading position, drawn as a rule across the feed. */}
      <div className="lastcheck" data-settled={settled}>
        <span>{lastCheckedPhrase(feed?.since)}</span>
      </div>

      {settled ? (
        <div className="empty">
          <div className="empty-mark" aria-hidden="true" />
          <p>
            <strong>Nothing needs you right now.</strong> Your watchlist has moved
            only within its normal range since your last visit. Anything unusual
            will appear here the moment it happens.
          </p>
        </div>
      ) : (
        <ul className="feed">
          {entries.map((entry) => {
            const key = entry.signal_id ?? `drift-${entry.ticker}`
            const direction = entry.change_pct >= 0 ? 'up' : 'down'
            return (
              <li className="entry" key={key} data-severity={entry.severity}>
                <div className="entry-body">
                  <div className="entry-top">
                    <span className="tag" data-severity={entry.severity} data-kind={entry.kind}>
                      {TYPE_LABEL[entry.type] ?? entry.type.toLowerCase()}
                    </span>
                    <span className="entry-when">{relativeTime(entry.at)}</span>
                  </div>

                  <p className="entry-headline">{entry.headline}</p>
                  <p className="entry-detail">{entry.detail}</p>

                  {entry.kind === 'event' && (
                    <SigmaMeter magnitude={entry.magnitude} severity={entry.severity} />
                  )}
                </div>

                <div className="entry-side">
                  <span className={`entry-change num ${direction}`}>
                    {entry.change_pct >= 0 ? '+' : ''}
                    {entry.change_pct.toFixed(2)}%
                  </span>
                  {entry.price != null && (
                    <span className="entry-price num">
                      {entry.price.toLocaleString('en-IN', {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })}
                    </span>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
