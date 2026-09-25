import { useQuery } from '@tanstack/react-query'
import { Activity } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { scalpingApi } from '@/api/scalping'
import { Navbar } from '@/components/layout/Navbar'
import { DepthTable } from '@/components/scalping/DepthTable'
import { ScalpChart } from '@/components/scalping/ScalpChart'
import { SetSLDialog } from '@/components/scalping/SetSLDialog'
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
import { findLegSL, useTrailingSL, type SLState } from '@/hooks/useTrailingSL'
import { priceDecimals } from '@/lib/scalpingPrice'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'
import type { OptionChainRow, ScalpingAction, ScalpingProduct, SelectedLeg } from '@/types/scalping'
import { showToast } from '@/utils/toast'

const DEFAULT_STRIKE_COUNT = 10
const MAX_LOTS = 20
const ORDER_COOLDOWN_MS = 120
const ARMED_STORAGE_KEY = 'scalping.armed'
const CHARTS_STORAGE_KEY = 'scalping.showCharts'
const CHART_TF_STORAGE_KEY = 'scalping.chartTf'
const CHART_TIMEFRAMES = ['1m', '5m', '15m'] as const

type ScalpingExchange = 'NFO' | 'BFO' | 'MCX' | 'CDS'
const EXCHANGES: ScalpingExchange[] = ['NFO', 'BFO', 'MCX', 'CDS']

const DEFAULT_UNDERLYING: Record<string, string> = {
  NFO: 'NIFTY',
  BFO: 'SENSEX',
  MCX: 'CRUDEOIL',
  CDS: 'USDINR',
}

function loadArmed(): boolean {
  try {
    return localStorage.getItem(ARMED_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

function loadShowCharts(): boolean {
  try {
    return localStorage.getItem(CHARTS_STORAGE_KEY) !== '0'
  } catch {
    return true
  }
}

function loadChartTf(): string {
  try {
    const v = localStorage.getItem(CHART_TF_STORAGE_KEY)
    if (v && (CHART_TIMEFRAMES as readonly string[]).includes(v)) return v
  } catch {
    // ignore
  }
  return '1m'
}

/**
 * One column of the 3x2 grid. In OPTIONS mode the three columns are the CE
 * leg, the underlying, and the PE leg; in FUTURES mode they are the near,
 * mid and far contracts of the picked underlying.
 */
interface GridCell {
  id: string
  label: string
  symbol: string
  exchange: string
  /** Spot/index columns have no order book and no order buttons. */
  tradable: boolean
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

export default function Scalping() {
  const apiKey = useAuthStore((s) => s.apiKey)
  const appMode = useThemeStore((s) => s.appMode)

  // Exchange / segment
  const [exchange, setExchange] = useState<ScalpingExchange>('NFO')
  const [segment, setSegment] = useState<'OPTIONS' | 'FUTURES'>('OPTIONS')
  const optionsMode = segment === 'OPTIONS'

  // Underlying & strikes
  const [underlying, setUnderlying] = useState<string>(DEFAULT_UNDERLYING.NFO)
  const [expiry, setExpiry] = useState<string>('')
  const [ceStrike, setCeStrike] = useState<string>('')
  const [peStrike, setPeStrike] = useState<string>('')

  // Order controls
  const [armed, setArmed] = useState<boolean>(loadArmed)
  const [lots, setLots] = useState(1)
  const [product, setProduct] = useState<ScalpingProduct>('NRML')
  const [showCharts, setShowCharts] = useState<boolean>(loadShowCharts)
  const [chartTf, setChartTf] = useState<string>(loadChartTf)

  // Books dock
  const [bookTab, setBookTab] = useState<'positions' | 'orders' | 'trades'>('positions')

  // SL dialog
  const [slDialog, setSlDialog] = useState<{
    leg: SelectedLeg
    side: ScalpingAction
    entry: number
    qty: number
  } | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(ARMED_STORAGE_KEY, armed ? '1' : '0')
    } catch {
      // ignore
    }
  }, [armed])

  useEffect(() => {
    try {
      localStorage.setItem(CHARTS_STORAGE_KEY, showCharts ? '1' : '0')
    } catch {
      // ignore
    }
  }, [showCharts])

  useEffect(() => {
    try {
      localStorage.setItem(CHART_TF_STORAGE_KEY, chartTf)
    } catch {
      // ignore
    }
  }, [chartTf])

  // Exchange switch resets the whole context: NIFTY has no contracts on
  // MCX/BFO, so carrying it across pointed every query at the wrong universe.
  useEffect(() => {
    setUnderlying(DEFAULT_UNDERLYING[exchange] ?? 'NIFTY')
    setExpiry('')
    setCeStrike('')
    setPeStrike('')
  }, [exchange])

  // A picked underlying must never be queried against a stale expiry.
  useEffect(() => {
    setExpiry('')
    setCeStrike('')
    setPeStrike('')
  }, [underlying])

  // Underlyings list
  const undInstrumentType = segment === 'FUTURES' ? 'futures' : 'options'
  const { data: allUndResp } = useQuery({
    queryKey: ['scalping', 'allunderlyings', exchange, undInstrumentType],
    queryFn: () => scalpingApi.getAllUnderlyings(exchange, undInstrumentType),
    staleTime: 5 * 60 * 1000,
  })
  const allUnderlyings = allUndResp?.data ?? []

  // Expiries
  const { data: expiryResp } = useQuery({
    queryKey: ['scalping', 'expiry', exchange, underlying],
    queryFn: () => scalpingApi.getExpiry(underlying, exchange, 'options'),
    enabled: optionsMode && !!underlying,
  })
  const expiries = expiryResp?.data ?? []
  useEffect(() => {
    if (optionsMode && underlying && !expiry && expiries.length > 0) {
      setExpiry(expiries[0])
    }
  }, [expiries, underlying, expiry, optionsMode])

  // Option chain
  const { data: chainResp } = useQuery({
    queryKey: ['scalping', 'strikes', exchange, underlying, expiry],
    queryFn: () => scalpingApi.getStrikes(underlying, exchange, expiry, DEFAULT_STRIKE_COUNT),
    enabled: optionsMode && !!underlying && !!expiry,
  })
  const chain = useMemo(() => chainResp?.chain ?? [], [chainResp])
  const foExchange = chainResp?.fo_exchange ?? exchange
  const underlyingSym = chainResp?.underlying_symbol ?? underlying
  const underlyingExch = chainResp?.underlying_exchange ?? exchange

  // ATM default + keep picks inside the chain
  useEffect(() => {
    if (chainResp?.atm_strike == null || chain.length === 0) return
    const strikes = new Set(chain.map((r) => String(r.strike)))
    const atm = String(chainResp.atm_strike)
    setCeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
    setPeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
  }, [chainResp, chain])

  const ceLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === ceStrike), 'ce', foExchange),
    [chain, ceStrike, foExchange]
  )
  const peLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === peStrike), 'pe', foExchange),
    [chain, peStrike, foExchange]
  )

  // Futures contracts (near / mid / far for the three columns)
  const { data: futResp } = useQuery({
    queryKey: ['scalping', 'futures', exchange, underlying],
    queryFn: () => scalpingApi.futures(underlying, exchange),
    enabled: segment === 'FUTURES' && !!underlying,
  })
  const futContracts = futResp?.data ?? []

  // The six grid cells.
  const cells: GridCell[] = useMemo(() => {
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
    (cell: GridCell): SelectedLeg | null => {
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

  // ── Live feed: every cell + books refresh on order events ────────────
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

  // ── Books (positions / orders / trades) ──────────────────────────────
  const { data: posResp } = useQuery({
    queryKey: ['scalping', 'positions', appMode],
    queryFn: () => scalpingApi.getTracked(),
    refetchInterval: 4000,
  })
  const positionsBook = posResp?.data ?? []

  const { slMap, setSL, clearSL } = useTrailingSL(appMode)

  // ── Orders ───────────────────────────────────────────────────────────
  const lastFireRef = useRef(0)
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null)

  const submitOrder = useCallback(
    async (leg: SelectedLeg | null, action: ScalpingAction) => {
      if (!armed) {
        showToast.error('One-Click is off — enable it to trade', 'orders')
        return
      }
      if (!leg) {
        showToast.error('No instrument selected', 'orders')
        return
      }
      if (!leg.lotsize) {
        showToast.error('Lot/contract size unavailable for this instrument', 'orders')
        return
      }
      const now = Date.now()
      if (now - lastFireRef.current < ORDER_COOLDOWN_MS) return
      lastFireRef.current = now

      const quantity = lots * leg.lotsize
      const t0 = performance.now()
      try {
        const res = await scalpingApi.placeOrder({
          symbol: leg.symbol,
          exchange: leg.exchange,
          action,
          quantity,
          product,
          lots,
        })
        setLastLatencyMs(Math.round(performance.now() - t0))
        if (res.status !== 'success') {
          showToast.error(res.message ?? 'Order failed', 'orders')
          return
        }
        // Offer the SL dialog pre-filled at this fill.
        const ltp = getTick(leg.symbol, leg.exchange)?.ltp
        if (ltp && ltp > 0) {
          setSlDialog({ leg, side: action, entry: ltp, qty: quantity })
        }
      } catch (e) {
        setLastLatencyMs(Math.round(performance.now() - t0))
        const err = e as { response?: { data?: { message?: string } }; message?: string }
        showToast.error(err.response?.data?.message || err.message || 'Order failed', 'orders')
      }
    },
    [armed, lots, product, getTick]
  )

  const onSaveSL = useCallback(
    (sl: SLState) => {
      setSL(sl)
      setSlDialog(null)
    },
    [setSL]
  )

  // ── Render helpers ───────────────────────────────────────────────────
  const dec = (exch?: string) => priceDecimals(exch ?? exchange)

  const legHeader = (cell: GridCell) => {
    const leg = cellLeg(cell)
    const tick = getTick(cell.symbol, cell.exchange)
    const isCe = cell.id === 'ce'
    const isPe = cell.id === 'pe'
    return (
      <div className="flex items-center gap-1.5 shrink-0 pb-1 border-b border-border/50">
        <span
          className={cn(
            'rounded font-bold text-xs px-1.5 py-0.5',
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
            <SelectTrigger className="h-6 w-32 text-xs font-mono font-bold px-1.5">
              <SelectValue placeholder="Strike" />
            </SelectTrigger>
            <SelectContent className="max-h-60 text-xs">
              {chain.map((r) => (
                <SelectItem key={`${cell.id}-${r.strike}`} value={String(r.strike)} className="text-xs font-mono">
                  {r.strike}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <span className="text-xs font-semibold font-mono truncate" title={cell.symbol}>
            {cell.label}
          </span>
        )}
        <div className="flex items-baseline gap-1 ml-auto font-mono">
          <span className="text-sm font-bold">
            {tick?.ltp != null ? tick.ltp.toFixed(dec(cell.exchange)) : '—'}
          </span>
          {lastLatencyMs != null && cell.id === 'ce' && (
            <span className="text-[10px] text-muted-foreground">{lastLatencyMs}ms</span>
          )}
        </div>
        {leg && findLegSL(slMap, leg.symbol, leg.exchange, product) && (
          <Badge variant="outline" className="text-[9px] px-1 py-0">
            SL
          </Badge>
        )}
      </div>
    )
  }

  const legButtons = (cell: GridCell) => {
    const leg = cellLeg(cell)
    const existing = leg ? findLegSL(slMap, leg.symbol, leg.exchange, product) : undefined
    return (
      <div className="shrink-0 pt-1 border-t border-border/50 space-y-1">
        <div className="grid grid-cols-2 gap-1.5">
          <Button
            size="sm"
            disabled={!leg}
            className="h-8 bg-emerald-600 hover:bg-emerald-700 font-bold text-xs"
            onClick={() => submitOrder(leg, 'BUY')}
          >
            ↑ BUY
          </Button>
          <Button
            size="sm"
            disabled={!leg}
            className="h-8 bg-rose-600 hover:bg-rose-700 font-bold text-xs"
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
              setSlDialog({
                leg,
                side: existing?.side ?? 'BUY',
                entry: existing?.entry ?? ltp ?? 0,
                qty: existing?.quantity ?? lots * leg.lotsize,
              })
            }}
          >
            {existing ? 'Edit SL' : 'Set SL'}
          </Button>
          {existing && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs text-rose-600"
              onClick={() => leg && clearSL(leg.symbol, leg.exchange, product)}
            >
              Clear SL
            </Button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <Navbar />
      <div className="flex flex-1 flex-col gap-1 overflow-hidden p-1.5 min-h-0">
        {/* Ribbon */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 shrink-0">
          <div className="flex items-center gap-1.5">
            <span className="font-bold text-sm tracking-tight text-foreground flex items-center gap-1">
              <Activity className="h-4 w-4 text-primary" /> Scalper
            </span>
            <Badge
              variant={armed ? 'destructive' : 'secondary'}
              className="cursor-pointer text-[10px] px-1.5 py-0"
              onClick={() => setArmed(!armed)}
            >
              {armed ? 'ON' : 'OFF'}
            </Badge>
          </div>

          <div className="h-4 w-px shrink-0 bg-border/60" />

          <div className="flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Exch</span>
            <Select value={exchange} onValueChange={(v) => setExchange(v as ScalpingExchange)}>
              <SelectTrigger className="h-7 w-16 text-xs px-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EXCHANGES.map((x) => (
                  <SelectItem key={x} value={x} className="text-xs">
                    {x}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Seg</span>
            <Select value={segment} onValueChange={(v) => setSegment(v as 'OPTIONS' | 'FUTURES')}>
              <SelectTrigger className="h-7 w-20 text-xs px-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="OPTIONS" className="text-xs">Options</SelectItem>
                <SelectItem value="FUTURES" className="text-xs">Futures</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Und</span>
            <select
              className="h-7 w-32 rounded border border-border bg-background px-1.5 font-mono text-xs font-bold"
              value={underlying}
              onChange={(e) => setUnderlying(e.target.value)}
            >
              {!allUnderlyings.includes(underlying) && (
                <option value={underlying}>{underlying}</option>
              )}
              {allUnderlyings.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
          </div>

          {optionsMode && (
            <div className="flex items-center gap-1">
              <span className="text-[11px] text-muted-foreground">Exp</span>
              <Select value={expiry} onValueChange={setExpiry} disabled={!underlying}>
                <SelectTrigger className="h-7 w-24 text-xs px-1.5">
                  <SelectValue placeholder="Expiry" />
                </SelectTrigger>
                <SelectContent>
                  {expiries.map((e) => (
                    <SelectItem key={e} value={e} className="text-xs font-mono">
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Lots</span>
            <div className="flex items-center rounded border border-border/80 bg-background">
              <button
                type="button"
                onClick={() => setLots((n) => Math.max(1, n - 1))}
                className="px-1.5 py-0.5 text-xs hover:bg-muted text-muted-foreground"
              >
                −
              </button>
              <span className="w-6 text-center font-mono font-bold text-xs tabular-nums">{lots}</span>
              <button
                type="button"
                onClick={() => setLots((n) => Math.min(MAX_LOTS, n + 1))}
                className="px-1.5 py-0.5 text-xs hover:bg-muted text-muted-foreground"
              >
                +
              </button>
            </div>
          </div>

          <Select value={product} onValueChange={(v) => setProduct(v as ScalpingProduct)}>
            <SelectTrigger className="h-7 w-16 text-xs px-1.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="NRML" className="text-xs">NRML</SelectItem>
              <SelectItem value="MIS" className="text-xs">MIS</SelectItem>
            </SelectContent>
          </Select>

          <div className="h-4 w-px shrink-0 bg-border/60" />

          <Select value={chartTf} onValueChange={setChartTf}>
            <SelectTrigger className="h-7 w-16 text-xs px-1.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CHART_TIMEFRAMES.map((tf) => (
                <SelectItem key={tf} value={tf} className="text-xs">
                  {tf}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button
            variant={showCharts ? 'secondary' : 'outline'}
            size="sm"
            className="h-7 text-xs"
            onClick={() => setShowCharts((v) => !v)}
          >
            Charts
          </Button>
        </div>

        {/* 3x2 grid: CE / spot / PE charts on top, depth + orders below */}
        <div
          className="grid flex-1 min-h-0 gap-1.5"
          style={{
            gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
            gridTemplateRows: showCharts ? 'minmax(0, 3fr) minmax(0, 2fr)' : 'minmax(0, 1fr)',
          }}
        >
          {cells.map((cell) => {
            const leg = cellLeg(cell)
            const showTopChart = showCharts
            return (
              <div
                key={cell.id}
                className={cn(
                  'min-h-0 min-w-0 flex flex-col rounded-md border border-border/80 bg-card p-1.5 overflow-hidden',
                  showCharts ? 'row-span-2' : ''
                )}
                style={showCharts ? { gridRow: 'span 2' } : undefined}
              >
                {legHeader(cell)}
                {showTopChart && (
                  <div className="min-h-0 flex-1 my-1 rounded overflow-hidden border border-border/50 bg-background/50">
                    <ScalpChart
                      symbol={cell.symbol}
                      exchange={cell.exchange}
                      interval={chartTf}
                    />
                  </div>
                )}
                {!showTopChart && <div className="flex-1" />}
                <div className="shrink-0">
                  <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between">
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
                {cell.tradable ? (
                  legButtons(cell)
                ) : (
                  <div className="shrink-0 pt-1 border-t border-border/50 text-center text-[10px] text-muted-foreground py-2">
                    {cell.id === 'spot' ? 'Underlying — no orders' : 'Not selected'}
                  </div>
                )}
                {leg && (
                  <p className="sr-only">
                    {leg.symbol} on {leg.exchange}, lot size {leg.lotsize}
                  </p>
                )}
              </div>
            )
          })}
        </div>

        {/* Books dock */}
        <div className="shrink-0 rounded-md border border-border/80 bg-card">
          <div className="flex items-center gap-2 px-2 py-1 border-b border-border/50">
            {(['positions', 'orders', 'trades'] as const).map((t) => (
              <button
                key={t}
                type="button"
                className={cn(
                  'text-[11px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded',
                  bookTab === t ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'
                )}
                onClick={() => setBookTab(t)}
              >
                {t}
              </button>
            ))}
            <span className="ml-auto text-[10px] text-muted-foreground">
              {bookTab === 'positions'
                ? `${positionsBook.length} scalping instrument(s)`
                : bookTab === 'orders'
                  ? 'press F7 semantics — cancel via Close-All'
                  : 'fills for scalping orders'}
            </span>
          </div>
          <div className="max-h-32 overflow-y-auto px-2 py-1 text-[11px] font-mono">
            {bookTab === 'positions' &&
              (positionsBook.length === 0 ? (
                <p className="text-muted-foreground">Nothing traded yet today.</p>
              ) : (
                positionsBook.map((t: { symbol: string; exchange: string; product: string }) => (
                  <p key={`${t.exchange}:${t.symbol}:${t.product}`}>
                    {t.exchange}:{t.symbol} · {t.product}
                  </p>
                ))
              ))}
            {bookTab !== 'positions' && (
              <p className="text-muted-foreground">
                Full order &amp; trade books live on the dashboard — this dock keeps the
                scalping list so Close-All stays scoped.
              </p>
            )}
          </div>
        </div>
      </div>

      <SetSLDialog
        open={!!slDialog}
        onOpenChange={(o) => {
          if (!o) setSlDialog(null)
        }}
        leg={slDialog?.leg ?? null}
        product={product}
        side={slDialog?.side ?? 'BUY'}
        entryPrice={slDialog?.entry ?? 0}
        quantity={slDialog?.qty ?? 0}
        ltp={slDialog?.entry}
        existing={
          slDialog
            ? findLegSL(slMap, slDialog.leg.symbol, slDialog.leg.exchange, product)
            : undefined
        }
        onSave={onSaveSL}
        onClear={() => {
          if (slDialog) clearSL(slDialog.leg.symbol, slDialog.leg.exchange, product)
          setSlDialog(null)
        }}
      />
    </div>
  )
}
