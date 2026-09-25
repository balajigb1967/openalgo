/**
 * ScalperGrid — the CE / underlying / PE instrument strip for a scalper
 * workspace.
 *
 * Given an underlying + expiry it resolves the option chain, renders three
 * chart columns (CE, underlying, PE), and beneath each a matching 5-level
 * market depth with Buy/Sell and SL controls. Futures mode swaps the CE/PE
 * columns for the near/mid/far contracts. It is layout-agnostic: /scalping
 * stacks it full-height, /trading's "Scalper" preset embeds it as the grid.
 */

import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { scalpingApi } from '@/api/scalping'
import { DepthTable } from '@/components/scalping/DepthTable'
import { ScalpChart } from '@/components/scalping/ScalpChart'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useMarketData } from '@/hooks/useMarketData'
import { findLegSL, type SLState } from '@/hooks/useTrailingSL'
import { priceDecimals } from '@/lib/scalpingPrice'
import { cn } from '@/lib/utils'
import type { OptionChainRow, ScalpingAction, ScalpingProduct, SelectedLeg } from '@/types/scalping'

const DEFAULT_STRIKE_COUNT = 10
const ORDER_COOLDOWN_MS = 120

export type ScalperSegment = 'OPTIONS' | 'FUTURES'
export type ScalperExchange = 'NFO' | 'BFO' | 'MCX' | 'CDS'

export const SCALPER_EXCHANGES: ScalperExchange[] = ['NFO', 'BFO', 'MCX', 'CDS']
export const SCALPER_DEFAULT_UNDERLYING: Record<string, string> = {
  NFO: 'NIFTY',
  BFO: 'SENSEX',
  MCX: 'CRUDEOIL',
  CDS: 'USDINR',
}

export interface ScalperGridCell {
  id: string
  label: string
  symbol: string
  exchange: string
  tradable: boolean
}

export interface ScalperGridProps {
  apiKey: string | null | undefined
  exchange: ScalperExchange
  segment: ScalperSegment
  underlying: string
  chartTf: string
  lots: number
  product: ScalpingProduct
  armed: boolean
  showCharts: boolean
  slMap: Record<string, SLState>
  onSetSL: (sl: SLState) => void
  onClearSL: (symbol: string, exchange: string, product: string) => void
  onOrderResult?: (ok: boolean, message?: string) => void
  /** Fires on a fill and on Set-SL, so the host can open the SL dialog. */
  onSLRequest?: (leg: SelectedLeg, side: ScalpingAction, entry: number, qty: number) => void
  /** A synced strike (watchlist click / advisor alert) to select once the chain resolves. */
  pendingStrike?: { side: 'CE' | 'PE'; strike: number } | null
  compact?: boolean
}

function buildLeg(
  row: OptionChainRow | undefined,
  type: 'ce' | 'pe',
  foExchange: string
): SelectedLeg | null {
  if (!row) return null
  const leg = row[type]
  if (!leg?.symbol) return null
  return {
    symbol: leg.symbol,
    exchange: foExchange,
    optionType: type.toUpperCase() as 'CE' | 'PE',
    strike: row.strike,
    lotsize: leg.lotsize ?? 0,
    tickSize: leg.tick_size ?? 0,
  }
}

export function ScalperGrid({
  apiKey,
  exchange,
  segment,
  underlying,
  chartTf,
  lots,
  product,
  armed,
  showCharts,
  slMap,
  // onSetSL stays in the props contract for hosts that persist SL edits
  // locally; the grid only opens the dialog through onSLRequest.
  onClearSL,
  onOrderResult,
  onSLRequest,
  pendingStrike,
  compact = false,
}: ScalperGridProps) {
  const optionsMode = segment === 'OPTIONS'
  const [expiry, setExpiry] = useState('')
  const [ceStrike, setCeStrike] = useState('')
  const [peStrike, setPeStrike] = useState('')
  const lastFireRef = useRef(0)

  // A picked underlying must never be queried against a stale expiry.
  useEffect(() => {
    setExpiry('')
    setCeStrike('')
    setPeStrike('')
  }, [underlying])

  const { data: expiryResp } = useQuery({
    queryKey: ['scalpergrid', 'expiry', exchange, underlying],
    queryFn: () => scalpingApi.getExpiry(underlying, exchange, 'options'),
    enabled: optionsMode && !!underlying,
  })
  const expiries = expiryResp?.data ?? []
  useEffect(() => {
    if (optionsMode && underlying && !expiry && expiries.length > 0) setExpiry(expiries[0])
  }, [expiries, underlying, expiry, optionsMode])

  const { data: chainResp } = useQuery({
    queryKey: ['scalpergrid', 'strikes', exchange, underlying, expiry],
    queryFn: () => scalpingApi.getStrikes(underlying, exchange, expiry, DEFAULT_STRIKE_COUNT),
    enabled: optionsMode && !!underlying && !!expiry,
  })
  const chain = useMemo(() => chainResp?.chain ?? [], [chainResp])
  const foExchange = chainResp?.fo_exchange ?? exchange
  const underlyingSym = chainResp?.underlying_symbol ?? underlying
  const underlyingExch = chainResp?.underlying_exchange ?? exchange

  const pendingRef = useRef<{ side: 'CE' | 'PE'; strike: number } | null>(null)
  useEffect(() => {
    if (pendingStrike) pendingRef.current = pendingStrike
  }, [pendingStrike])

  useEffect(() => {
    if (chainResp?.atm_strike == null || chain.length === 0) return
    const strikes = new Set(chain.map((r) => String(r.strike)))
    const atm = String(chainResp.atm_strike)
    setCeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
    setPeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
    // A synced strike wins over the ATM default (advisor alert / watchlist pick).
    const pend = pendingRef.current
    if (pend && strikes.has(String(pend.strike))) {
      if (pend.side === 'CE') setCeStrike(String(pend.strike))
      else setPeStrike(String(pend.strike))
      pendingRef.current = null
    }
  }, [chainResp, chain])

  const ceLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === ceStrike), 'ce', foExchange),
    [chain, ceStrike, foExchange]
  )
  const peLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === peStrike), 'pe', foExchange),
    [chain, peStrike, foExchange]
  )

  const { data: futResp } = useQuery({
    queryKey: ['scalpergrid', 'futures', exchange, underlying],
    queryFn: () => scalpingApi.futures(underlying, exchange),
    enabled: segment === 'FUTURES' && !!underlying,
  })
  const futContracts = futResp?.data ?? []

  const cells: ScalperGridCell[] = useMemo(() => {
    if (optionsMode) {
      return [
        {
          id: 'ce',
          label: `CE ${ceStrike}`,
          symbol: ceLeg?.symbol ?? '',
          exchange: ceLeg?.exchange ?? foExchange,
          tradable: !!ceLeg,
        },
        {
          id: 'spot',
          label: underlying,
          symbol: underlyingSym,
          exchange: underlyingExch,
          tradable: false,
        },
        {
          id: 'pe',
          label: `PE ${peStrike}`,
          symbol: peLeg?.symbol ?? '',
          exchange: peLeg?.exchange ?? foExchange,
          tradable: !!peLeg,
        },
      ]
    }
    return [0, 1, 2].map((i) => {
      const c = futContracts[i]
      return {
        id: `fut${i}`,
        label: c ? c.expiry : `month ${i + 1}`,
        symbol: c?.symbol ?? '',
        exchange: c ? exchange : '',
        tradable: !!c,
      }
    })
  }, [optionsMode, ceStrike, peStrike, ceLeg, peLeg, foExchange, underlying, underlyingSym, underlyingExch, futContracts, exchange])

  const cellLeg = useCallback(
    (cell: ScalperGridCell): SelectedLeg | null => {
      if (!cell.tradable || !cell.symbol) return null
      if (cell.id === 'ce') return ceLeg
      if (cell.id === 'pe') return peLeg
      const c = futContracts.find((f) => f.symbol === cell.symbol)
      return {
        symbol: cell.symbol,
        exchange: cell.exchange,
        optionType: 'CE',
        strike: 0,
        lotsize: c?.lotsize ?? 0,
        tickSize: c?.tick_size ?? 0.05,
      }
    },
    [ceLeg, peLeg, futContracts]
  )

  const feedSymbols = useMemo(
    () =>
      cells
        .filter((c) => c.symbol && c.exchange)
        .map((c) => ({ symbol: c.symbol, exchange: c.exchange })),
    [cells]
  )
  const { data } = useMarketData({
    symbols: feedSymbols,
    mode: 'Quote',
    enabled: feedSymbols.length > 0,
    autoReconnect: true,
  })
  const getTick = useCallback(
    (symbol?: string, exch?: string) =>
      symbol && exch ? data.get(`${exch}:${symbol}`)?.data : undefined,
    [data]
  )

  const submitOrder = useCallback(
    async (leg: SelectedLeg | null, action: ScalpingAction) => {
      if (!armed) {
        onOrderResult?.(false, 'One-Click is off — enable it to trade')
        return
      }
      if (!leg || !leg.lotsize) {
        onOrderResult?.(false, 'No instrument selected')
        return
      }
      const now = Date.now()
      if (now - lastFireRef.current < ORDER_COOLDOWN_MS) return
      lastFireRef.current = now

      const quantity = lots * leg.lotsize
      try {
        const res = await scalpingApi.placeOrder({
          symbol: leg.symbol,
          exchange: leg.exchange,
          action,
          quantity,
          product,
          lots,
        })
        if (res.status === 'success') {
          onOrderResult?.(true)
          const ltp = getTick(leg.symbol, leg.exchange)?.ltp
          if (ltp && ltp > 0) onSLRequest?.(leg, action, ltp, quantity)
        } else {
          onOrderResult?.(false, res.message ?? 'Order failed')
        }
      } catch (e) {
        const err = e as { response?: { data?: { message?: string } }; message?: string }
        onOrderResult?.(false, err.response?.data?.message || err.message || 'Order failed')
      }
    },
    [armed, lots, product, getTick, onOrderResult, onSLRequest]
  )

  const dec = (exch?: string) => priceDecimals(exch ?? exchange)

  // h-full (not flex-1): the host may be a block container (/trading's pane),
  // where flex-1 without a flex parent collapses instead of filling.
  return (
    <div
      className="grid h-full min-h-0 min-w-0 gap-1.5"
      style={{
        gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
        gridTemplateRows: showCharts ? 'minmax(0, 3fr) minmax(0, 2fr)' : 'minmax(0, 1fr)',
      }}
    >
      {cells.map((cell) => {
        const leg = cellLeg(cell)
        const tick = getTick(cell.symbol, cell.exchange)
        const existing = leg ? findLegSL(slMap, leg.symbol, leg.exchange, product) : undefined
        const isCe = cell.id === 'ce'
        const isPe = cell.id === 'pe'
        return (
          <div
            key={cell.id}
            className="flex min-h-0 min-w-0 flex-col rounded-md border border-border/80 bg-card p-1.5"
          >
            {/* Header */}
            <div className="flex shrink-0 items-center gap-1.5 border-b border-border/50 pb-1">
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 text-xs font-bold',
                  isCe && 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
                  isPe && 'bg-rose-500/15 text-rose-600 dark:text-rose-400',
                  !isCe && !isPe && 'bg-primary/10 text-primary'
                )}
              >
                {isCe ? 'CE' : isPe ? 'PE' : cell.id === 'spot' ? 'SPOT' : 'FUT'}
              </span>
              {optionsMode && (isCe || isPe) ? (
                <Select
                  value={isCe ? ceStrike : peStrike}
                  onValueChange={isCe ? setCeStrike : setPeStrike}
                  disabled={chain.length === 0}
                >
                  <SelectTrigger className="h-6 w-32 px-1.5 font-mono text-xs font-bold">
                    <SelectValue placeholder="Strike" />
                  </SelectTrigger>
                  <SelectContent className="max-h-60 text-xs">
                    {chain.map((r) => (
                      <SelectItem
                        key={`${cell.id}-${r.strike}`}
                        value={String(r.strike)}
                        className="font-mono text-xs"
                      >
                        {r.strike}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <span className="truncate font-mono text-xs font-semibold" title={cell.symbol}>
                  {cell.label}
                </span>
              )}
              <div className="ml-auto flex items-baseline gap-1 font-mono">
                <span className="text-sm font-bold">
                  {tick?.ltp != null ? tick.ltp.toFixed(dec(cell.exchange)) : '—'}
                </span>
              </div>
              {leg && existing && (
                <Badge variant="outline" className="px-1 py-0 text-[9px]">
                  SL
                </Badge>
              )}
            </div>

            {/* Chart */}
            {showCharts ? (
              <div className="my-1 min-h-0 flex-1 overflow-hidden rounded border border-border/50 bg-background/50">
                <ScalpChart symbol={cell.symbol} exchange={cell.exchange} interval={chartTf} />
              </div>
            ) : (
              <div className="flex-1" />
            )}

            {/* Depth */}
            <div className="shrink-0">
              <div className="mb-0.5 flex justify-between px-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                <span>Market Depth</span>
                <span className="font-mono normal-case">{cell.symbol}</span>
              </div>
              <DepthTable
                apiKey={apiKey}
                symbol={cell.symbol}
                exchange={cell.exchange}
                enabled={!!cell.symbol}
              />
            </div>

            {/* Orders */}
            {cell.tradable ? (
              <div className="shrink-0 space-y-1 border-t border-border/50 pt-1">
                <div className="grid grid-cols-2 gap-1.5">
                  <Button
                    size="sm"
                    disabled={!leg}
                    className={cn('bg-emerald-600 font-bold text-xs hover:bg-emerald-700', compact ? 'h-7' : 'h-8')}
                    onClick={() => submitOrder(leg, 'BUY')}
                  >
                    ↑ BUY
                  </Button>
                  <Button
                    size="sm"
                    disabled={!leg}
                    className={cn('bg-rose-600 font-bold text-xs hover:bg-rose-700', compact ? 'h-7' : 'h-8')}
                    onClick={() => submitOrder(leg, 'SELL')}
                  >
                    ↓ SELL
                  </Button>
                </div>
                <div className="flex items-center gap-1.5">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!leg}
                    className="h-7 flex-1 text-xs"
                    onClick={() => {
                      if (!leg) return
                      const ltp = getTick(leg.symbol, leg.exchange)?.ltp
                      onSLRequest?.(
                        leg,
                        existing?.side ?? 'BUY',
                        existing?.entry ?? ltp ?? 0,
                        existing?.quantity ?? lots * leg.lotsize
                      )
                    }}
                  >
                    {existing ? 'Edit SL' : 'Set SL'}
                  </Button>
                  {existing && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs text-rose-600"
                      onClick={() => {
                        if (!leg) return
                        onClearSL(leg.symbol, leg.exchange, product)
                      }}
                    >
                      Clear
                    </Button>
                  )}
                </div>
              </div>
            ) : (
              <div className="shrink-0 border-t border-border/50 py-2 text-center text-[10px] text-muted-foreground">
                {cell.id === 'spot' ? 'Underlying — no orders' : 'Not selected'}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
