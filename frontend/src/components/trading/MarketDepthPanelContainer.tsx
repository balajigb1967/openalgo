import { useEffect, useRef, useState } from 'react'
import { MarketDepthPanel } from './MarketDepthPanel'
import type { DepthLevel } from './MarketDepthPanel'
import { tradingApi } from '@/api/trading'
import type { DepthData } from '@/api/trading'
import { MarketDataManager, type SymbolData } from '@/lib/MarketDataManager'
import { cn } from '@/lib/utils'

/**
 * Sidebar market depth — hybrid live feed.
 *
 * Base layer: REST /api/v1/depth polled every 2s (verified live against the
 * broker — the book quantities move between polls). Upgrade layer: when the
 * shared MarketDataManager websocket pushes depth ticks for the symbol they
 * overwrite the poll instantly and the chip flips to TICK. The Fyers MCX
 * depth stream does not currently emit, so the poll is what keeps the panel
 * live; the websocket path lights up automatically wherever the broker feed
 * works.
 */

type FeedState = 'connecting' | 'live' | 'poll'

function toLevels(list: Array<{ price: number; quantity: number; orders?: number }> | undefined) {
  return (list ?? []).map((l) => ({
    price: Number(l.price) || 0,
    quantity: Number(l.quantity) || 0,
    orders: l.orders,
  }))
}

function convert(data: SymbolData | undefined): { buy: DepthLevel[]; sell: DepthLevel[] } | null {
  const buy = toLevels(data?.data?.depth?.buy)
  const sell = toLevels(data?.data?.depth?.sell)
  if (!buy.length && !sell.length) return null
  return { buy, sell }
}

export function MarketDepthPanelContainer({
  apiKey,
  wsUrl: _wsUrl,
  symbol,
  exchange,
}: {
  apiKey: string
  wsUrl: string
  symbol: string
  exchange: string
}) {
  const [depth, setDepth] = useState<{ buy: DepthLevel[]; sell: DepthLevel[] } | null>(null)
  const [feed, setFeed] = useState<FeedState>('connecting')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const aliveRef = useRef(true)
  const lastWsRef = useRef(0)
  const POLL_MS = 2000

  useEffect(() => {
    aliveRef.current = true
    setDepth(null)
    setLoading(true)
    setError(null)
    setFeed('connecting')

    if (!symbol || !exchange) {
      setError('No symbol selected — focus a chart pane with a symbol.')
      setLoading(false)
      return
    }

    const manager = MarketDataManager.getInstance()
    const applyWs = (data: SymbolData) => {
      const d = convert(data)
      if (!d || !aliveRef.current) return
      lastWsRef.current = Date.now()
      setDepth(d)
      setFeed('live')
      setError(null)
      setLoading(false)
    }

    const wsUnsub = manager.subscribe(symbol, exchange, 'Depth', applyWs)

    // Kick the manager's connect lifecycle (no-op when already connected).
    manager.connect().catch(() => {
      /* the state listener below reports the failure */
    })

    const stateUnsub = manager.addStateListener((state) => {
      if (!aliveRef.current) return
      if (state.isFallbackMode) setFeed((f) => (f === 'live' ? f : 'poll'))
    })

    // Base layer: silent REST poll every 2s. No spinner toggling — the book
    // just updates in place; the chip tells the user which feed is driving it.
    const poll = async () => {
      try {
        const response = await tradingApi.getDepth(apiKey, symbol, exchange)
        if (!aliveRef.current) return
        if (response.status === 'success') {
          const depthData = response.data as DepthData
          const converted = {
            buy: toLevels(depthData.bids),
            sell: toLevels(depthData.asks),
          }
          if (converted.buy.length || converted.sell.length) {
            setDepth(converted)
            setFeed(() => (Date.now() - lastWsRef.current < 10_000 ? "live" : "poll"))
            setError(null)
            setLoading(false)
          }
        }
      } catch {
        /* transient network/broker errors: the next poll retries */
      }
    }
    poll()
    const pollTimer = setInterval(poll, POLL_MS)

    return () => {
      aliveRef.current = false
      clearInterval(pollTimer)
      wsUnsub()
      stateUnsub()
    }
  }, [apiKey, symbol, exchange])

  const refetchRest = async () => {
    setLoading(true)
    try {
      const response = await tradingApi.getDepth(apiKey, symbol, exchange)
      if (response.status === 'success') {
        const depthData = response.data as DepthData
        setDepth({
          buy: toLevels(depthData.bids),
          sell: toLevels(depthData.asks),
        })
        setFeed(() => (Date.now() - lastWsRef.current < 10_000 ? "live" : "poll"))
      } else {
        setError(`Failed to load depth: ${response.message}`)
      }
    } catch {
      setError('Failed to load market depth data')
    } finally {
      setLoading(false)
    }
  }

  if (error) {
    return (
      <div className="p-4 text-center text-sm text-destructive">
        {error}
      </div>
    )
  }

  if (loading || !depth) {
    return (
      <div className="p-4 text-center text-sm text-muted-foreground">
        Loading live depth for {exchange && `${exchange}:`}{symbol}…
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between pb-2 border-b">
        <h3 className="font-medium text-lg">Market Depth</h3>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {exchange && `${exchange}:`}{symbol}
          <button
            type="button"
            onClick={refetchRest}
            className={cn(
              'flex items-center gap-1 rounded border px-1.5 py-px text-[10px] font-semibold',
              feed === 'live' && 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
              feed === 'poll' && 'border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400',
              feed === 'connecting' && 'border-border text-muted-foreground'
            )}
            title={
              feed === 'live'
                ? 'Streaming from the broker websocket — updates on every tick'
                : feed === 'poll'
                  ? 'Live book polled every 2 seconds (broker websocket not pushing depth for this symbol)'
                  : 'Connecting…'
            }
          >
            <span
              className={cn(
                'h-1.5 w-1.5 rounded-full',
                feed === 'live' && 'animate-pulse bg-emerald-500',
                feed === 'poll' && 'animate-pulse bg-sky-500',
                feed === 'connecting' && 'animate-pulse bg-muted-foreground'
              )}
            />
            {feed === 'live' ? 'TICK' : feed === 'poll' ? 'LIVE · 2s' : '…'}
          </button>
        </div>
      </div>
      <MarketDepthPanel depth={depth} isExpanded={true} onToggle={() => {}} maxLevels={5} />
    </div>
  )
}
