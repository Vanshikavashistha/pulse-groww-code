import React from 'react'

/* The significance band.
 *
 * Everything this product does rests on one number: how far a symbol has
 * moved relative to how it normally moves. So that number is the first thing
 * on the screen rather than something buried in an alert.
 *
 * Each bar is one symbol, its height is the live z-score, and the rule across
 * the band is the 2σ alert threshold. A bar under the line is a stock behaving
 * normally; a bar through it is the reason something appears in the feed.
 * Watching a bar rise toward the line is the system's judgement made visible.
 */

const CEILING = 4.5 // sigmas mapped to full bar height

export default function PulseBand({ items, threshold = 2 }) {
  if (!items.length) return null

  const linePosition = Math.min(threshold / CEILING, 0.92) * 100
  const breaching = items.filter((item) => item.z >= threshold).length

  return (
    <section className="band" aria-label="Live significance by symbol">
      <div className="band-head">
        <h2>Significance now</h2>
        <p>
          {breaching === 0
            ? 'Every symbol is inside its own normal range'
            : `${breaching} ${breaching === 1 ? 'symbol is' : 'symbols are'} outside their normal range`}
        </p>
      </div>

      <div className="band-plot">
        <div className="band-threshold" style={{ bottom: `${linePosition}%` }}>
          <span>{threshold}σ</span>
        </div>

        <div className="band-bars">
          {items.map((item) => {
            const height = Math.min(item.z / CEILING, 1) * 100
            const state = !item.warm
              ? 'learning'
              : item.z >= threshold
                ? (item.change_pct ?? 0) >= 0 ? 'up' : 'down'
                : 'quiet'
            return (
              <div className="band-column" key={item.ticker}>
                <div className="band-track">
                  <div
                    className="band-bar"
                    data-state={state}
                    style={{ height: `${Math.max(height, 2)}%` }}
                    title={
                      item.warm
                        ? `${item.ticker}: ${item.z.toFixed(2)}σ from its own average move`
                        : `${item.ticker}: still learning this symbol's volatility`
                    }
                  />
                </div>
                <span className="band-sigma num" data-state={state}>
                  {item.warm ? `${item.z.toFixed(1)}σ` : '—'}
                </span>
                <span className="band-ticker">{item.ticker}</span>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
