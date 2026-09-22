/**
 * ScalperChart — renders a lightweight-charts candlestick + volume chart
 * powered by ScalpChart for a scalper terminal column (CE / SPOT / PE).
 *
 * Lightweight, fast, automatically resizing with container dimensions,
 * adaptive lookback history (1d/3d/9d), and live websocket tick streaming.
 */

import { ScalpChart } from '@/components/scalping/ScalpChart'

export function ScalperChart({
  symbol,
  exchange,
  interval = '1m',
  columnId,
}: {
  apiKey?: string
  wsUrl?: string
  symbol: string
  exchange: string
  interval?: string
  columnId?: string
}) {
  return (
    <ScalpChart
      symbol={symbol}
      exchange={exchange}
      interval={interval}
      title={columnId ? columnId.toUpperCase() : undefined}
    />
  )
}
