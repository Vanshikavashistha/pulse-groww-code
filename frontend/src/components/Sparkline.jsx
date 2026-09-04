import React from 'react'

/* A price shape, not a chart.
 *
 * No axes, no gridlines, no tooltip. Its only job is to answer "has this been
 * drifting or lurching?" at a glance while the eye is really on the number
 * next to it. Anything more would compete with the feed for attention, which
 * is the one thing this interface must not do.
 */
export default function Sparkline({ points, direction = 'up', width = 62, height = 20 }) {
  if (!points || points.length < 2) {
    return <svg width={width} height={height} aria-hidden="true" />
  }

  const min = Math.min(...points)
  const max = Math.max(...points)
  const range = max - min || 1
  const step = width / (points.length - 1)

  const path = points
    .map((value, index) => {
      const x = index * step
      // Inset by a pixel top and bottom so the stroke is never clipped.
      const y = height - 1 - ((value - min) / range) * (height - 2)
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <svg
      className="spark"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      data-direction={direction}
      aria-hidden="true"
    >
      <path d={path} fill="none" strokeWidth="1.5" strokeLinejoin="round"
            strokeLinecap="round" />
    </svg>
  )
}
