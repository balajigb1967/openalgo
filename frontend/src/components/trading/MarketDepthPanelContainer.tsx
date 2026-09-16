import { useEffect, useRef, useState } from 'react'
import { MarketDepthPanel } from './MarketDepthPanel'
import type { DepthLevel } from './MarketDepthPanel'
import { tradingApi } from '@/api/trading'
import type { DepthData } from '@/api/trading'
import { MarketDataManager, type SymbolData } from '@/lib/MarketDataManager'
import { cn } from '@/lib/utils'

/**
 * Sidebar market depth, live.
 *
 * Data comes from the shared MarketDataManager WebSocket (Depth mode) — the
 * same stream the chart and option chain use, so the book updates on every
 * broker tick instead of a fixed 5-second REST poll. The REST /depth endpoint
 * is used for an initial fill (and the header chip's manual refresh); after
 * that the websocket keeps it live, including outside market hours via the
 * manager's REST-fallback mode.
 */

type FeedState = 'connecting' | 'live' | 'rest'

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
  const restTried = useRef(false)
  const aliveRef = useRef(true)
  const hasDepthRef = useRef(false)

  useEffect(() => {
    aliveRef.current = true
    restTried.current = false
    hasDepthRef.current = false
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
      hasDepthRef.current = true
      setDepth(d)
      setFeed(data.updateSource === 'rest' ? 'rest' : 'live')
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
      if (state.isFallbackMode) setFeed('rest')
    })

    // One-time REST fill: an immediate book for context. Never loops — the
    // websocket owns updates after this.
    const restFill = async () => {
      if (restTried.current) return
      restTried.current = true
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
            setDepth((prev) => prev ?? converted)
            setFeed((f) => (f === 'connecting' ? 'rest' : f))
            setLoading(false)
          }
        }
      } catch {
        /* silent: the websocket path owns the error surface */
      }
    }
    restFill()
    // One more REST attempt only if nothing has arrived within 4s (covers
    // slow broker feed negotiation).
    const retry = setTimeout(() => {
      if (aliveRef.current && !hasDepthRef.current) {
        restTried.current = false
        restFill()
      }
    }, 4000)

    return () => {
      aliveRef.current = false
      clearTimeout(retry)
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
        setFeed((f) => (f === 'connecting' ? 'rest' : f))
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
              feed === 'rest' && 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
              feed === 'connecting' && 'border-border text-muted-foreground'
            )}
            title={
              feed === 'live'
                ? 'Streaming live from the broker websocket — updates on every tick'
                : feed === 'rest'
                  ? 'Websocket unavailable — showing a REST snapshot. Click to refresh.'
                  : 'Connecting…'
            }
          >
            <span
              className={cn(
                'h-1.5 w-1.5 rounded-full',
                feed === 'live' && 'animate-pulse bg-emerald-500',
                feed === 'rest' && 'bg-amber-500',
                feed === 'connecting' && 'animate-pulse bg-muted-foreground'
              )}
            />
            {feed === 'live' ? 'LIVE' : feed === 'rest' ? 'SNAPSHOT' : '…'}
          </button>
        </div>
      </div>
      <MarketDepthPanel depth={depth} isExpanded={true} onToggle={() => {}} maxLevels={5} />
    </div>
  )
}
