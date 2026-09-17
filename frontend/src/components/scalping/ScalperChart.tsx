import { useCallback, useEffect, useRef, useState } from 'react'
import { TradingTerminal, type SearchRow } from '@/lib/trading/terminal'
import { useThemeStore } from '@/stores/themeStore'

/**
 * ScalperChart — one column's chart, running the same OpenAlgo charting
 * engine (TradingTerminal: openalgo-charts canvas, OpenAlgoDataFeed history,
 * WebSocket candle builder, OHLC legend) as the main Trading grid.
 *
 * The engine owns the bar pipeline end to end; this wrapper only mounts a
 * fresh terminal per symbol/exchange change and reports connection state.
 * A terminal per symbol (rather than loadSymbol on a long-lived one) keeps
 * the columns isolated — each carries its own subscription, drawings
 * namespace and legend, exactly like a grid pane.
 */

export function ScalperChart({
  apiKey,
  wsUrl,
  symbol,
  exchange,
  columnId,
}: {
  apiKey: string
  wsUrl: string
  symbol: string
  exchange: string
  /** Unique per column (ce/spot/pe) — namespaces the engine's localStorage workspace. */
  columnId: string
}) {
  const chartRef = useRef<HTMLDivElement>(null)
  const legendRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<'loading' | 'live' | 'down'>('loading')

  const onWsState = useCallback((s: string) => {
    if (s === 'open') setState('live')
    else if (s === 'closed' || s === 'error' || s === 'auth failed') setState((p) => (p === 'live' ? 'live' : 'down'))
  }, [])

  useEffect(() => {
    const chartEl = chartRef.current
    const legendEl = legendRef.current
    if (!chartEl || !legendEl || !apiKey || !wsUrl) return

    let alive = true
    let terminal: TradingTerminal | null = null
    setState('loading')

    const boot = async () => {
      terminal = new TradingTerminal({
        apiKey,
        wsUrl,
        container: chartEl,
        legendEl,
        storageKey: `oa-scalper-${columnId}`,
        getTheme: () => {
          const s = useThemeStore.getState()
          return { mode: s.mode, appMode: s.appMode }
        },
        callbacks: {
          onReady: () => {
            if (alive) setState((p) => (p === 'down' ? 'down' : 'live'))
          },
          onWsState,
          onToast: () => {
            /* toasts are host-level noise in a small column; drop them */
          },
          onLtp: () => {
            /* the column header reads prices from the shared feed, not here */
          },
          onSymbolLoaded: () => {
            if (alive) setState('live')
          },
        },
      })
      terminal.init()
      // init() restores its default symbol; re-aim at the column's contract
      // once the engine is up so the chart shows what the header picked.
      if (symbol && exchange) {
        await terminal
          .loadSymbol({ symbol, exchange } satisfies SearchRow)
          .catch(() => {})
      }
      if (alive) setState((p) => (p === 'loading' ? 'live' : p))
    }
    void boot()

    return () => {
      alive = false
      terminal?.destroy()
      terminal = null
    }
  }, [apiKey, wsUrl, symbol, exchange, columnId, onWsState])

  return (
    <div className="relative h-full w-full" data-scalper-chart={symbol ? `${exchange}:${symbol}` : 'empty'}>
      <div ref={chartRef} className="absolute inset-0" />
      <div
        ref={legendRef}
        className="pointer-events-none absolute left-1.5 top-1 z-10 text-[10px] font-medium text-foreground"
      />
      {(!symbol || state === 'down') && (
        <div className="absolute bottom-1 right-1.5 z-10 rounded bg-muted/70 px-1 py-px text-[8px] font-semibold text-muted-foreground">
          {!symbol ? 'no instrument' : state === 'down' ? 'feed down' : ''}
        </div>
      )}
    </div>
  )
}
