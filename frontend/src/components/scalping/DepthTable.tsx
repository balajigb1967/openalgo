import { useEffect, useRef, useState } from 'react'
import { type DepthData, tradingApi } from '@/api/trading'

interface DepthTableProps {
  apiKey?: string | null
  symbol: string
  exchange: string
  enabled?: boolean
  className?: string
}

/**
 * 5-level market depth table with live quantity bars.
 *
 * Polls the REST depth endpoint at 5s intervals (respecting the 180-200/min quota).
 * Displays best 5 bids and asks with quantities, prices, and proportional volume depth bars.
 */
export function DepthTable({
  apiKey,
  symbol,
  exchange,
  enabled = true,
  className = '',
}: DepthTableProps) {
  const [depth, setDepth] = useState<DepthData | null>(null)
  const [dead, setDead] = useState(false)
  const lastRef = useRef('')

  useEffect(() => {
    if (!enabled || !symbol || !exchange || !apiKey) return
    let alive = true
    let timer: number | undefined

    const fetchDepth = async () => {
      try {
        const res = await tradingApi.getDepth(apiKey, symbol, exchange)
        if (!alive) return
        if (res.status !== 'success' || !res.data) {
          setDead(true)
          return
        }
        const d = res.data
        setDead(false)
        const sig = JSON.stringify([d.bids?.[0], d.asks?.[0], d.ltp])
        if (sig !== lastRef.current) {
          lastRef.current = sig
          setDepth(d)
        }
      } catch {
        // Transient error — retain last book
      }
    }

    fetchDepth()
    timer = window.setInterval(fetchDepth, 5000)
    return () => {
      alive = false
      if (timer) window.clearInterval(timer)
    }
  }, [apiKey, symbol, exchange, enabled])

  useEffect(() => {
    setDepth(null)
    setDead(false)
    lastRef.current = ''
  }, [symbol, exchange])

  if (!enabled || !symbol) {
    return (
      <div className={`flex min-h-[65px] flex-1 items-center justify-center rounded border border-border/40 text-[9px] text-muted-foreground ${className}`}>
        —
      </div>
    )
  }

  const bids = depth?.bids ?? []
  const asks = depth?.asks ?? []

  if (!bids.length && !asks.length) {
    const isIndex = (exchange === 'NSE' || exchange === 'BSE') && symbol.toUpperCase().includes('INDEX')
    return (
      <div className={`flex min-h-[65px] flex-1 items-center justify-center rounded border border-border/40 px-2 text-center text-[9px] text-muted-foreground ${className}`}>
        {dead ? 'Depth unavailable' : isIndex ? 'Spot index — no order book' : 'Waiting for depth…'}
      </div>
    )
  }

  const maxQ = Math.max(
    ...bids.map((b) => b.quantity || 0),
    ...asks.map((a) => a.quantity || 0),
    1
  )

  return (
    <div className={`min-h-[65px] flex-1 rounded border border-border/40 bg-muted/10 p-0.5 overflow-hidden ${className}`}>
      <table className="w-full table-fixed border-collapse text-[10px]">
        <thead>
          <tr className="text-[8px] uppercase tracking-wide text-muted-foreground border-b border-border/30">
            <th className="w-[26%] py-px px-1 text-left font-semibold">B-Qty</th>
            <th className="w-[24%] py-px px-1 text-right font-semibold text-emerald-600 dark:text-emerald-400">Bid</th>
            <th className="w-[24%] border-l border-border/40 py-px px-1 text-left font-semibold text-rose-600 dark:text-rose-400">Ask</th>
            <th className="w-[26%] py-px px-1 text-right font-semibold">A-Qty</th>
          </tr>
        </thead>
        <tbody className="font-mono tabular-nums text-[9px]">
          {[0, 1, 2, 3, 4].map((i) => {
            const b = bids[i]
            const a = asks[i]
            return (
              <tr key={i} className="border-b border-border/20 last:border-0 hover:bg-muted/30">
                <td className="relative px-1 py-px text-left font-medium text-emerald-600 dark:text-emerald-400 truncate">
                  {b && (
                    <span
                      className="absolute inset-y-0 left-0 bg-emerald-500/15"
                      style={{ width: `${((b.quantity || 0) / maxQ) * 100}%` }}
                    />
                  )}
                  <span className="relative">{b ? b.quantity.toLocaleString('en-IN') : '—'}</span>
                </td>
                <td className="px-1 py-px text-right font-semibold text-emerald-600 dark:text-emerald-400">
                  {b ? b.price : '—'}
                </td>
                <td className="border-l border-border/40 px-1 py-px text-left font-semibold text-rose-600 dark:text-rose-400">
                  {a ? a.price : '—'}
                </td>
                <td className="relative px-1 py-px text-right font-medium text-rose-600 dark:text-rose-400 truncate">
                  {a && (
                    <span
                      className="absolute inset-y-0 right-0 bg-rose-500/15"
                      style={{ width: `${((a.quantity || 0) / maxQ) * 100}%` }}
                    />
                  )}
                  <span className="relative">{a ? a.quantity.toLocaleString('en-IN') : '—'}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default DepthTable
