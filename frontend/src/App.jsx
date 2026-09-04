import React, { useCallback, useEffect, useRef, useState } from 'react'
import Feed from './components/Feed'
import PulseBand from './components/PulseBand'
import Watchlist from './components/Watchlist'
import { api, getHandle, setHandle, subscribe } from './api'

export default function App() {
  const [feed, setFeed] = useState(null)
  const [items, setItems] = useState([])
  const [zThreshold, setZThreshold] = useState(2)
  const [universe, setUniverse] = useState([])
  const [health, setHealth] = useState(null)
  const [connection, setConnection] = useState('connecting')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [marking, setMarking] = useState(false)
  const [handle, setHandleState] = useState(getHandle)

  // Guards against overlapping refreshes when a burst of SSE updates arrives
  // faster than the fetches complete.
  const refreshing = useRef(false)

  const refresh = useCallback(async () => {
    if (refreshing.current) return
    refreshing.current = true
    try {
      const [feedData, watchlistData] = await Promise.all([
        api.feed(),
        api.watchlist(),
      ])
      setFeed(feedData)
      setItems(watchlistData.items)
      if (watchlistData.z_threshold) setZThreshold(watchlistData.z_threshold)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      refreshing.current = false
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    api.universe().then((data) => setUniverse(data.symbols)).catch(() => {})
    api.health().then(setHealth).catch(() => {})
  }, [refresh, handle])

  useEffect(() => {
    const unsubscribe = subscribe(() => refresh(), setConnection)
    // A slow safety poll: if the stream silently dies behind a proxy the view
    // still converges rather than freezing on stale numbers.
    const timer = setInterval(refresh, 20000)
    return () => {
      unsubscribe()
      clearInterval(timer)
    }
  }, [refresh])

  const markSeen = async () => {
    if (!feed) return
    setMarking(true)
    try {
      // Send back the cursor this view actually rendered, so a signal
      // detected during the round trip is not silently consumed.
      await api.markSeen(feed.ack_cursor)
      await refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setMarking(false)
    }
  }

  const guard = (fn) => async (...args) => {
    try {
      await fn(...args)
      await refresh()
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }

  const changeHandle = (value) => {
    const next = value.trim() || 'demo'
    setHandle(next)
    setHandleState(next)
    setLoading(true)
  }

  const provider = health?.poller?.provider
  const breakerOpen = health?.poller?.breaker === 'open'

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <h1 className="wordmark">Pulse</h1>
          <p className="tagline">
            A watchlist that answers one question: what changed since you last
            looked?
          </p>
        </div>
        <div className="masthead-right">
          <span className="pulse-dot" data-state={breakerOpen ? 'offline' : connection}>
            {breakerOpen
              ? 'Feed interrupted, showing last known prices'
              : connection === 'live'
                ? `Live via ${provider ?? 'feed'}`
                : connection === 'reconnecting'
                  ? 'Reconnecting'
                  : 'Connecting'}
          </span>
          <input
            className="handle-field"
            value={handle}
            onChange={(event) => changeHandle(event.target.value)}
            aria-label="Your handle, which determines whose reading position you see"
            title="Change this to see a different user's unread state"
          />
        </div>
      </header>

      <PulseBand items={items} threshold={zThreshold} />

      <div className="columns">
        <Feed
          feed={feed}
          loading={loading}
          onMarkSeen={markSeen}
          marking={marking}
        />
        <Watchlist
          items={items}
          universe={universe}
          onAdd={guard(api.addSymbol)}
          onRemove={guard(api.removeSymbol)}
          onPin={guard(api.pinSymbol)}
          error={error}
        />
      </div>

      {health && (
        <p className="engine-note">
          A move is flagged when it exceeds{' '}
          <code>{health.thresholds.z_score}σ</code> of that symbol's own recent
          volatility, not a fixed percentage, so a quiet large-cap and a
          volatile small-cap are held to different standards. Volume is called
          unusual above <code>{health.thresholds.volume_surge}x</code> its
          rolling average. Any price older than{' '}
          <code>{health.thresholds.stale_after_seconds}s</code> is labelled
          rather than shown as current.
        </p>
      )}
    </div>
  )
}
