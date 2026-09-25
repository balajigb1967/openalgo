/**
 * ScalperOpenAlgoChart - the full OpenAlgo chart engine in a scalper cell.
 *
 * Replaces ScalpChart (lightweight-charts, candles only, no studies) with the
 * in-house engine: the widget's topbar carries the chart-type dropdown, the
 * interval pills and the Indicators button, and the `openalgo-charts/indicators`
 * tier registers the built-in studies (102) plus any user indicator modules —
 * so every chart type and every indicator the trading terminal offers is
 * available here too.
 *
 * Two adaptations for the scalper's cramped cells:
 *
 * - The engine's drawing rail and statusline are off. A rail would eat a
 *   sixth of each cell; the context menu still draws, and the topbar keeps
 *   the instrument's type/interval/indicators within reach.
 * - The engine tracks its own container size and needs a box with a real
 *   height, which is the absolute-fill the grid cell provides — the same
 *   arrangement ScalpChart used, so the cell markup does not change.
 *
 * The feed is the shared `scalperLiveFeed` instance: one websocket
 * subscription per instrument across every cell on the page, and a chart
 * that follows a strike or underlying change without being rebuilt, so the
 * studies and chart type the user chose stay on the chart.
 */

import { OpenAlgoChart, type ChartDataEvent } from '@/components/chart/OpenAlgoChart'
import { scalperLiveFeed } from '@/lib/chart/feeds/scalperLiveFeed'

/** Interval pills the topbar offers, matching the scalper ribbon's select. */
export const SCALPER_INTERVALS = ['1m', '5m', '15m'] as const

export function ScalperOpenAlgoChart({
  symbol,
  exchange,
  interval,
  onIntervalChange,
}: {
  symbol: string
  exchange: string
  interval: string
  /** Fired when the user picks a pill in the topbar; the host syncs its ribbon select. */
  onIntervalChange?: (interval: string) => void
}) {
  if (!symbol || !exchange) {
    return (
      <div className="flex h-full w-full items-center justify-center rounded-lg border bg-card text-xs text-muted-foreground">
        No instrument
      </div>
    )
  }

  return (    <OpenAlgoChart
      feed={scalperLiveFeed}
      symbol={symbol}
      exchange={exchange}
      interval={interval}
      intervals={SCALPER_INTERVALS}
      volume
      topbar
      rail={false}
      statusline={false}
      indicators
      className="h-full"
      onIntervalChange={onIntervalChange}
      onData={(e: ChartDataEvent) => {
        // The engine draws its own "No bars" state; nothing to add here, but
        // keeping the hook wired means a future status readout costs nothing.
        void e
      }}
    />
  )
}
