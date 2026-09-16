import { useEffect, useRef, useState } from 'react'
import { tradingApi } from '@/api/trading'

/**
 * Per-strike market-depth cell for the scalper chain table.
 *
 * The REST depth endpoint (the proven-live path on this deployment) is polled
 * at `pollMs` while mounted. Best bid / ask come from level 0 of the `bids` /
 * `asks` arrays in OpenAlgo's /depth payload. A quiet poll cycle (identical
 * payload) is skipped without a re-render.
 */
export function StrikeDepthCell({
  apiKey,
  symbol,
  exchange,
  pollMs = 3000,
}: {
  apiKey: string
  symbol: string
  exchange: string
  pollMs?: number
}) {
  const [bid, setBid] = useState<number | null>(null)
  const [ask, setAsk] = useState<number | null>(null)
  const lastRef = useRef('')

  useEffect(() => {
    if (!symbol || !exchange || !apiKey) return
    let alive = true
    let timer: number | undefined

    const fetchDepth = async () => {
      try {
        const res = await tradingApi.getDepth(apiKey, symbol, exchange)
        const d = res.data
        if (!alive || !d) return
        const top = (arr: Array<{ price: number }> | undefined) => arr?.[0]?.price ?? null
        const sig = JSON.stringify([top(d.bids), top(d.asks), d.ltp])
        if (sig === lastRef.current) return // no change since last poll
        lastRef.current = sig
        setBid(top(d.bids))
        setAsk(top(d.asks))
      } catch {
        // transient broker/API errors just leave the last values showing
      }
    }

    fetchDepth()
    timer = window.setInterval(fetchDepth, pollMs)
    return () => {
      alive = false
      if (timer) window.clearInterval(timer)
    }
  }, [apiKey, symbol, exchange, pollMs])

  if (bid == null && ask == null) {
    return <span className="text-[10px] text-muted-foreground">—</span>
  }
  return (
    <span className="flex flex-col items-end leading-[1.05] tabular-nums">
      <span className="text-[9px] font-medium text-emerald-600 dark:text-emerald-400">{bid ?? '—'}</span>
      <span className="text-[9px] font-medium text-rose-600 dark:text-rose-400">{ask ?? '—'}</span>
    </span>
  )
}

export default StrikeDepthCell
