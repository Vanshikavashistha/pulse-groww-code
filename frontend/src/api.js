// One place that knows how to talk to the server.
//
// The user handle lives in localStorage so that opening a second browser
// profile gives you a genuinely separate reading position -- which is the
// fastest way to see that unread state is per user, not global.

const HANDLE_KEY = 'pulse.handle'

export function getHandle() {
  let handle = localStorage.getItem(HANDLE_KEY)
  if (!handle) {
    handle = 'user-' + Math.random().toString(36).slice(2, 7)
    localStorage.setItem(HANDLE_KEY, handle)
  }
  return handle
}

export function setHandle(handle) {
  localStorage.setItem(HANDLE_KEY, handle)
}

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-User': getHandle(),
      ...(options.headers || {}),
    },
  })
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) message = body.detail
    } catch {
      // Non-JSON error body; the status message is all we have.
    }
    throw new Error(message)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  watchlist: () => request('/watchlist'),
  feed: () => request('/feed'),
  health: () => request('/health'),
  universe: () => request('/universe'),
  addSymbol: (ticker) =>
    request('/watchlist', { method: 'POST', body: JSON.stringify({ ticker }) }),
  removeSymbol: (ticker) =>
    request(`/watchlist/${ticker}`, { method: 'DELETE' }),
  pinSymbol: (ticker, pinned) =>
    request(`/watchlist/${ticker}/pin`, {
      method: 'POST',
      body: JSON.stringify({ pinned }),
    }),
  // ackCursor is the highest signal id the UI actually rendered, not the
  // server's current maximum. Anything detected mid-round-trip stays unread.
  markSeen: (ackCursor) =>
    request('/feed/seen', {
      method: 'POST',
      body: JSON.stringify({ ack_cursor: ackCursor }),
    }),
}

// Server-sent events. The stream carries only a version marker; on receiving
// one the app re-fetches through the normal endpoints, so there is a single
// code path that builds a feed and a reconnecting client always converges.
export function subscribe(onUpdate, onStatus) {
  const source = new EventSource('/api/stream')

  source.addEventListener('open', () => onStatus?.('live'))
  source.addEventListener('update', (event) => {
    try {
      onUpdate(JSON.parse(event.data))
    } catch {
      onUpdate({})
    }
  })
  source.addEventListener('error', () => {
    // EventSource reconnects on its own; we only reflect the state.
    onStatus?.(source.readyState === EventSource.CLOSED ? 'offline' : 'reconnecting')
  })

  return () => source.close()
}
