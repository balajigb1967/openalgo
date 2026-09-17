import { useCallback, useEffect, useRef, useState } from 'react'
import { TradingTerminal, type SearchRow } from '@/lib/trading/terminal'
import { useThemeStore } from '@/stores/themeStore'

/**
 * ScalperChart — one column's chart, running the same OpenAlgo charting
 * engine (TradingTerminal: openalgo-charts canvas, OpenAlgoDataFeed history,
 * WebSocket candle builder, OHLC legend) as the main Trading grid.
 *
 * Boot is once per column (apiKey/wsUrl/columnId); symbol switches reuse the
 * live terminal via loadSymbol instead of tearing it down, so a watchlist
 * click swaps the chart in one history fetch — no engine reboot, no wasted
 * restore/fallback load, no BHEL flash.
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
  const terminalRef = useRef<TradingTerminal | null>(null)
  /** `EXCHANGE:SYMBOL` the terminal is currently (or was last asked to be) showing. */
  const loadedRef = useRef('')
  const symbolRef = useRef(symbol)
  symbolRef.current = symbol
  const exchRef = useRef(exchange)
  exchRef.current = exchange
  const [state, setState] = useState<'loading' | 'live' | 'down'>('loading')

  const onWsState = useCallback((s: string) => {
    if (s === 'open') setState('live')
    else if (s === 'closed' || s === 'error' || s === 'auth failed') setState((p) => (p === 'live' ? 'live' : 'down'))
  }, [])

  /* ── boot: once per column ──────────────────────────────────────────── */
  useEffect(() => {
    const chartEl = chartRef.current
    const legendEl = legendRef.current
    if (!chartEl || !legendEl || !apiKey || !wsUrl) return

    let alive = true
    setState('loading')

    const boot = async () => {
      const first = symbolRef.current && exchRef.current
        ? { symbol: symbolRef.current, exchange: exchRef.current }
        : undefined
      loadedRef.current = first ? `${first.exchange}:${first.symbol}` : ''
      const terminal = new TradingTerminal({
        apiKey,
        wsUrl,
        container: chartEl,
        legendEl,
        storageKey: `oa-scalper-${columnId}`,
        // init() loads THIS instrument instead of restore/fallback-to-BHEL:
        // one history fetch, straight to the symbol the user picked.
        initialSymbol: first ?? undefined,
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
      terminalRef.current = terminal
      // init() resolves AFTER the first symbol's bars are requested, so the
      // chart below is never a stale instrument. A symbol picked while boot
      // was in flight is applied here (loadSymbol needs rest, set inside init).
      await terminal.init()
      if (!alive) return
      const want = symbolRef.current && exchRef.current
        ? `${exchRef.current}:${symbolRef.current}`
        : ''
      if (want && want !== loadedRef.current) {
        loadedRef.current = want
        await terminal
          .loadSymbol({ symbol: symbolRef.current, exchange: exchRef.current } satisfies SearchRow)
          .catch(() => {})
      }
      if (alive) setState((p) => (p === 'loading' ? 'live' : p))
    }
    void boot()

    return () => {
      alive = false
      terminalRef.current?.destroy()
      terminalRef.current = null
    }
  }, [apiKey, wsUrl, columnId, onWsState])

  /* ── symbol switches: reuse the live terminal ───────────────────────── */
  useEffect(() => {
    const t = terminalRef.current
    if (!t || !symbol || !exchange) return
    const key = `${exchange}:${symbol}`
    if (loadedRef.current === key) return
    loadedRef.current = key
    void t.loadSymbol({ symbol, exchange } satisfies SearchRow).catch(() => {})
  }, [symbol, exchange])

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
