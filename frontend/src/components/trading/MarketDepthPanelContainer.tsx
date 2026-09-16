import { useEffect, useState } from 'react'
import { MarketDepthPanel } from './MarketDepthPanel'
import type { DepthLevel } from './MarketDepthPanel'
import { tradingApi } from '@/api/trading'
import type { DepthData } from '@/api/trading'

export function MarketDepthPanelContainer({
  apiKey,
  wsUrl,
  symbol,
  exchange
}: {
  apiKey: string
  wsUrl: string
  symbol: string
  exchange: string
}) {
  const [depth, setDepth] = useState<{ buy: DepthLevel[]; sell: DepthLevel[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // Skip if no symbol/exchange provided
    if (!symbol || !exchange) {
      setError('No symbol selected')
      setLoading(false)
      return
    }

    // Fetch market depth data via trading API
    const fetchDepth = async () => {
      try {
        setLoading(true)
        setError(null)
        
        // Use the trading API to get market depth (apiKey, symbol, exchange)
        const response = await tradingApi.getDepth(apiKey, symbol, exchange)

        if (response.status === 'success') {
          const depthData = response.data as DepthData
          // Convert API format to component format
          const convertedDepth = {
            buy: depthData.bids.map((level) => ({
              price: level.price,
              quantity: level.quantity
            })),
            sell: depthData.asks.map((level) => ({
              price: level.price,
              quantity: level.quantity
            }))
          }
          setDepth(convertedDepth)
        } else {
          setError(`Failed to load market depth: ${response.message}`)
        }
      } catch (err) {
        setError('Failed to load market depth data')
        console.error('Market depth error:', err)
      } finally {
        setLoading(false)
      }
    }

    // Initial fetch
    fetchDepth()
    
    // Refresh every 5 seconds
    const interval = setInterval(fetchDepth, 5000)
    return () => clearInterval(interval)
  }, [apiKey, wsUrl, symbol, exchange])

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
        Loading market depth for {exchange && `${exchange}:`}{symbol}...
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between pb-2 border-b">
        <h3 className="font-medium text-lg">Market Depth</h3>
        <div className="text-xs text-muted-foreground">
          {exchange && `${exchange}:`}{symbol} • Updated every 5s
        </div>
      </div>
      <MarketDepthPanel depth={depth} isExpanded={true} onToggle={() => {}} maxLevels={5} />
    </div>
  )
}