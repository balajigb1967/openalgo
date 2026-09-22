import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Bookmark,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useDefaultLayout } from 'react-resizable-panels'
import { scalperApi, type ScalperAlert } from '@/api/scalper-orderflow'
import { scalpingApi } from '@/api/scalping'
import { type QuotesData, tradingApi } from '@/api/trading'
import { watchlistApi } from '@/api/watchlist'
import { Navbar } from '@/components/layout/Navbar'
import { DepthTable } from '@/components/scalping/DepthTable'
import { ScalpChart } from '@/components/scalping/ScalpChart'
import { SetSLDialog } from '@/components/scalping/SetSLDialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useMarketData } from '@/hooks/useMarketData'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import { findLegSL, useTrailingSL } from '@/hooks/useTrailingSL'
import { priceDecimals } from '@/lib/scalpingPrice'
import { buildPositionRows } from '@/lib/scalpingRows'
import { mergeTick, type TickView } from '@/lib/scalpingTick'
import {
  getSyncTarget,
  type ScalperSyncState,
  type ScalperTarget,
  setSyncSymbol,
  setSyncTarget,
  subscribeSync,
  subscribeSyncTarget,
} from '@/lib/scalperSync'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'
import type {
  OptionChainRow,
  OptionType,
  ScalpingAction,
  ScalpingPositionRow,
  ScalpingProduct,
  SearchInstrument,
  Segment,
  SelectedLeg,
} from '@/types/scalping'
import { showToast } from '@/utils/toast'

const DEFAULT_STRIKE_COUNT = 10
const MAX_LOTS = 20
const ORDER_COOLDOWN_MS = 120
const ARMED_STORAGE_KEY = 'scalping.armed'
const CHARTS_STORAGE_KEY = 'scalping.showCharts'
const CHART_TF_STORAGE_KEY = 'scalping.chartTf'
const CHART_TIMEFRAMES = ['1m', '5m', '15m'] as const

type ScalpingExchange = 'NSE' | 'BSE' | 'NFO' | 'BFO' | 'MCX' | 'CDS'
const EXCHANGES: ScalpingExchange[] = ['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS']

const DEFAULT_UNDERLYING: Record<string, string> = {
  NFO: 'NIFTY',
  BFO: 'SENSEX',
  MCX: 'CRUDEOIL',
  CDS: 'USDINR',
}

const isEquityExchange = (e: ScalpingExchange) => e === 'NSE' || e === 'BSE'

const isTodayTs = (ts?: string): boolean => {
  if (!ts) return true
  const todayKey = new Date().toLocaleDateString('en-CA')
  if (/^\d{4}-\d{2}-\d{2}/.test(ts)) return ts.slice(0, 10) === todayKey
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return true
  return d.toLocaleDateString('en-CA') === todayKey
}

const BOOK_EVENTS = [
  'order_event',
  'analyzer_update',
  'close_position_event',
  'cancel_order_event',
  'modify_order_event',
] as const

const TICK_STALE_MS = 5000
const REFRESH_THROTTLE_MS = 400

interface SLTarget {
  symbol: string
  exchange: string
  product: ScalpingProduct
  optionType: OptionType
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
    return localStorage.getItem(CHARTS_STORAGE_KEY) === '1'
  } catch {
    return false
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

function apiErrorMessage(e: unknown): string {
  const err = e as { response?: { data?: { message?: string } }; message?: string }
  return err.response?.data?.message || err.message || 'Order failed'
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

const pctInRange = (v: number, low: number, high: number) =>
  high > low ? Math.min(100, Math.max(0, ((v - low) / (high - low)) * 100)) : 50

function RangeBarCompact({
  ltp,
  open,
  high,
  low,
  decimals = 2,
}: {
  ltp?: number
  open?: number
  high?: number
  low?: number
  decimals?: number
}) {
  if (ltp == null || high == null || low == null || high <= low) {
    return <div className="h-1 w-full bg-border/40 rounded my-1" />
  }
  const ltpPct = pctInRange(ltp, low, high)
  const openPct = open != null ? pctInRange(open, low, high) : null
  return (
    <div className="my-0.5">
      <div className="relative h-1.5 rounded bg-muted/60">
        {openPct != null && (
          <span
            className="-translate-x-1/2 -translate-y-1/2 absolute top-1/2 h-2 w-2 rounded-full border border-muted-foreground bg-background"
            style={{ left: `${openPct}%` }}
            title={`Open: ${open?.toFixed(decimals)}`}
          />
        )}
        <span
          className="-translate-x-1/2 -top-1 absolute text-[8px] text-foreground font-bold"
          style={{ left: `${ltpPct}%` }}
          title={`LTP: ${ltp.toFixed(decimals)}`}
        >
          ▼
        </span>
      </div>
      <div className="flex justify-between font-mono text-[9px] text-muted-foreground mt-0.5">
        <span>L: {low.toFixed(decimals)}</span>
        {open != null && <span>O: {open.toFixed(decimals)}</span>}
        <span>H: {high.toFixed(decimals)}</span>
      </div>
    </div>
  )
}

function cleanRoot(s: string): string {
  return s
    .replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d*/gi, '')
    .replace(/\d+$/, '')
    .trim()
}

const safeLayoutStorage = {
  getItem: (key: string): string | null => {
    try {
      return localStorage.getItem(key)
    } catch {
      return null
    }
  },
  setItem: (key: string, value: string): void => {
    try {
      localStorage.setItem(key, value)
    } catch {
      // ignore
    }
  },
}

export default function Scalping() {
  const apiKey = useAuthStore((s) => s.apiKey)
  const appMode = useThemeStore((s) => s.appMode)
  const queryClient = useQueryClient()

  // Layout persistence for dragging & resizing
  const verticalLayout = useDefaultLayout({
    id: 'oa-scalper-v3-dock',
    storage: safeLayoutStorage,
  })
  const optionsColumnsLayout = useDefaultLayout({
    id: 'oa-scalper-v3-opt-cols',
    storage: safeLayoutStorage,
  })
  const equityColumnsLayout = useDefaultLayout({
    id: 'oa-scalper-v3-eq-cols',
    storage: safeLayoutStorage,
  })

  // Exchange / segment
  const [exchange, setExchange] = useState<ScalpingExchange>('NFO')
  const [segment, setSegment] = useState<Segment>('OPTIONS')
  const isEquityExch = isEquityExchange(exchange)
  const optionsMode = !isEquityExch && segment === 'OPTIONS'

  // Underlying & strikes
  const [underlying, setUnderlying] = useState<string>(DEFAULT_UNDERLYING.NFO)
  const [underlyingQuery, setUnderlyingQuery] = useState('')
  const [underlyingOpen, setUnderlyingOpen] = useState(false)
  const [expiry, setExpiry] = useState<string>('')
  const [ceStrike, setCeStrike] = useState<string>('')
  const [peStrike, setPeStrike] = useState<string>('')

  // Equity instrument
  const [searchQuery, setSearchQuery] = useState('')
  const [instrument, setInstrument] = useState<SearchInstrument | null>(null)
  const [equityShares, setEquityShares] = useState(1)

  // Order controls
  const [armed, setArmed] = useState<boolean>(loadArmed)
  const [lots, setLots] = useState(1)
  const [product, setProduct] = useState<ScalpingProduct>('NRML')
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null)

  // Charts
  const [showCharts, setShowCharts] = useState<boolean>(loadShowCharts)
  const [chartTf, setChartTf] = useState<string>(loadChartTf)

  // Predefined SL / Target
  const [predefSlOn, setPredefSlOn] = useState(false)
  const [predefSlValue, setPredefSlValue] = useState('')
  const [predefSlUnit, setPredefSlUnit] = useState<'PTS' | 'PCT'>('PTS')
  const [predefTgtOn, setPredefTgtOn] = useState(false)
  const [predefTgtValue, setPredefTgtValue] = useState('')
  const [predefTgtUnit, setPredefTgtUnit] = useState<'PTS' | 'PCT'>('PTS')

  // Sync state
  const pendingStrikeRef = useRef<{ side: 'CE' | 'PE'; strike: number; underlying: string } | null>(null)
  const [syncSeq, setSyncSeq] = useState(0)
  const [syncBanner, setSyncBanner] = useState<string | null>(null)
  const syncBannerTimer = useRef<number | undefined>(undefined)

  const showSyncBanner = useCallback((msg: string) => {
    setSyncBanner(msg)
    if (syncBannerTimer.current) window.clearTimeout(syncBannerTimer.current)
    syncBannerTimer.current = window.setTimeout(() => setSyncBanner(null), 6000)
  }, [])

  // Books dock active tab
  const [bookTab, setBookTab] = useState<'positions' | 'orders' | 'trades'>('positions')

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

  // Exchange reset
  useEffect(() => {
    if (isEquityExch) {
      setSegment('EQUITY')
    } else {
      setSegment((s) => (s === 'OPTIONS' || s === 'FUTURES' ? s : 'OPTIONS'))
      setUnderlying((u) => u || DEFAULT_UNDERLYING[exchange] || 'NIFTY')
    }
    setInstrument(null)
    setSearchQuery('')
    setUnderlyingQuery('')
    setExpiry('')
    setCeStrike('')
    setPeStrike('')
  }, [exchange, isEquityExch])

  useEffect(() => {
    setProduct(isEquityExch ? 'MIS' : 'NRML')
  }, [isEquityExch])

  // Watchlists query for quick picker
  const { data: watchlistData } = useQuery({
    queryKey: ['scalping', 'watchlists'],
    queryFn: () => watchlistApi.list(),
  })
  const watchlists = watchlistData ?? []

  // Advisor alerts query for quick picker
  const { data: advisorData } = useQuery({
    queryKey: ['scalping', 'advisor'],
    queryFn: () => scalperApi.getAdvisor(),
    refetchInterval: 15000,
  })
  const activeAlerts: ScalperAlert[] = advisorData?.monitor?.alerts ?? []

  // Equity search
  const { data: eqSearchResp } = useQuery({
    queryKey: ['scalping', 'eqsearch', exchange, searchQuery],
    queryFn: () => scalpingApi.search(exchange, searchQuery),
    enabled: isEquityExch && searchQuery.trim().length >= 2,
  })
  const equityResults = eqSearchResp?.data ?? []

  // Underlyings list
  const undInstrumentType = segment === 'FUTURES' ? 'futures' : 'options'
  const { data: allUndResp } = useQuery({
    queryKey: ['scalping', 'allunderlyings', exchange, undInstrumentType],
    queryFn: () => scalpingApi.getAllUnderlyings(exchange, undInstrumentType),
    enabled: !isEquityExch,
    staleTime: 5 * 60 * 1000,
  })
  const allUnderlyings = allUndResp?.data ?? []
  const underlyingMatches = useMemo(() => {
    const q = underlyingQuery.trim().toUpperCase()
    const list = q ? allUnderlyings.filter((u) => u.toUpperCase().includes(q)) : allUnderlyings
    return list.slice(0, 100)
  }, [allUnderlyings, underlyingQuery])

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

  // Option Chain
  const { data: chainResp } = useQuery({
    queryKey: ['scalping', 'strikes', exchange, underlying, expiry],
    queryFn: () => scalpingApi.getStrikes(underlying, exchange, expiry, DEFAULT_STRIKE_COUNT),
    enabled: optionsMode && !!underlying && !!expiry,
  })
  const chain = useMemo(() => chainResp?.chain ?? [], [chainResp])
  const foExchange = chainResp?.fo_exchange ?? exchange
  const underlyingSym = chainResp?.underlying_symbol ?? underlying
  const underlyingExch = chainResp?.underlying_exchange ?? exchange

  // Futures
  const { data: futResp } = useQuery({
    queryKey: ['scalping', 'futures', exchange, underlying],
    queryFn: () => scalpingApi.futures(underlying, exchange),
    enabled: !isEquityExch && segment === 'FUTURES' && !!underlying,
  })
  const futContracts = futResp?.data ?? []

  useEffect(() => {
    if (isEquityExch || segment !== 'FUTURES' || futContracts.length === 0) return
    const stillValid = instrument && futContracts.some((c) => c.symbol === instrument.symbol)
    if (stillValid) return
    const c = futContracts[0]
    setInstrument({ symbol: c.symbol, exchange, lotsize: c.lotsize, name: underlying })
  }, [futContracts, isEquityExch, segment, instrument, exchange, underlying])

  // ATM / Pending strike resolution
  useEffect(() => {
    if (chainResp?.atm_strike == null || chain.length === 0) return
    const strikes = new Set(chain.map((r) => String(r.strike)))
    const atm = String(chainResp.atm_strike)
    const pend = pendingStrikeRef.current
    if (pend) {
      const chainUnd = String(chainResp.underlying_symbol ?? underlying ?? '').toUpperCase()
      if (chainUnd.includes(pend.underlying) || pend.underlying.includes(chainUnd)) {
        pendingStrikeRef.current = null
        const want = String(pend.strike)
        const has = strikes.has(want)
        if (pend.side === 'CE') {
          setCeStrike(has ? want : atm)
          setPeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
        } else {
          setPeStrike(has ? want : atm)
          setCeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
        }
        return
      }
    }
    setCeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
    setPeStrike((prev) => (prev && strikes.has(prev) ? prev : atm))
  }, [chainResp, chain, syncSeq, underlying])

  const ceLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === ceStrike), 'ce', foExchange),
    [chain, ceStrike, foExchange]
  )
  const peLeg = useMemo(
    () => buildLeg(chain.find((r) => String(r.strike) === peStrike), 'pe', foExchange),
    [chain, peStrike, foExchange]
  )
  const singleLeg: SelectedLeg | null = useMemo(() => {
    if (optionsMode || !instrument) return null
    return {
      symbol: instrument.symbol,
      exchange: instrument.exchange,
      optionType: 'CE',
      strike: 0,
      lotsize: instrument.lotsize ?? 1,
      tickSize: 0.05,
    }
  }, [optionsMode, instrument])

  // ── Sync with Watchlist & Scalper Advisor ───────────────────────────
  const applySyncSymbol = useCallback((s: ScalperSyncState) => {
    if (!s.symbol) return
    const rawExchange = s.symbol.includes(':') ? s.symbol.split(':')[0] : ''
    const rawSym = s.symbol.includes(':') ? s.symbol.split(':')[1] : s.symbol
    const upperExch = rawExchange.toUpperCase()

    if (upperExch === 'NSE' || upperExch === 'BSE') {
      const isIndex =
        rawSym.includes('INDEX') ||
        rawSym.includes('NIFTY') ||
        rawSym.includes('SENSEX') ||
        rawSym.includes('BANKNIFTY')
      if (isIndex) {
        const derivExch = upperExch === 'BSE' ? 'BFO' : 'NFO'
        setExchange(derivExch)
        setSegment('OPTIONS')
        const root = s.root || (rawSym.includes('BANK') ? 'BANKNIFTY' : rawSym.includes('SENSEX') ? 'SENSEX' : 'NIFTY')
        setUnderlying(root)
        showSyncBanner(`Watchlist: Synced index ${root} (${derivExch})`)
        return
      }
      setExchange(upperExch as ScalpingExchange)
      setSegment('EQUITY')
      setInstrument({ symbol: rawSym, exchange: upperExch, lotsize: 1, name: rawSym })
      showSyncBanner(`Watchlist: Synced ${upperExch}:${rawSym}`)
      return
    }

    const derivExch: ScalpingExchange = (['NFO', 'BFO', 'MCX', 'CDS'].includes(upperExch) ? upperExch : 'NFO') as ScalpingExchange
    setExchange(derivExch)

    if (s.optionType && s.strike != null && s.strike > 0) {
      setSegment('OPTIONS')
      const root = s.root || cleanRoot(rawSym)
      setUnderlying(root)
      pendingStrikeRef.current = { side: s.optionType, strike: s.strike, underlying: root.toUpperCase() }
      setSyncSeq((n) => n + 1)
      showSyncBanner(`Watchlist: Synced option ${rawSym} (${s.optionType} @${s.strike})`)
      return
    }

    if (rawSym.toUpperCase().endsWith('FUT')) {
      setSegment('FUTURES')
      const root = s.root || cleanRoot(rawSym.replace(/FUT$/i, ''))
      setUnderlying(root)
      showSyncBanner(`Watchlist: Synced future ${rawSym}`)
      return
    }

    setSegment('OPTIONS')
    const root = s.root || cleanRoot(rawSym)
    setUnderlying(root)
    showSyncBanner(`Watchlist: Synced underlying ${root}`)
  }, [showSyncBanner])

  const applySyncTarget = useCallback((t: ScalperTarget) => {
    const und = t.underlying || t.key
    if (!und) return
    const exch = (['NFO', 'BFO', 'MCX', 'CDS', 'NSE', 'BSE'].includes(t.exchange?.toUpperCase())
      ? t.exchange.toUpperCase()
      : 'NFO') as ScalpingExchange
    setExchange(exch)
    setSegment('OPTIONS')
    setUnderlying(und)
    if (t.side && t.strike != null && t.strike > 0) {
      pendingStrikeRef.current = { side: t.side, strike: t.strike, underlying: und.toUpperCase() }
      setSyncSeq((n) => n + 1)
    }
    showSyncBanner(`Advisor Alert: ${t.key} BUY ${t.side} @${t.strike}`)
  }, [showSyncBanner])

  useEffect(() => {
    const stored = getSyncTarget()
    if (stored) applySyncTarget(stored)
    return subscribeSyncTarget((t) => {
      if (t) applySyncTarget(t)
    })
  }, [applySyncTarget])

  useEffect(() => {
    return subscribeSync((s) => {
      if (s?.symbol) applySyncSymbol(s)
    })
  }, [applySyncSymbol])

  // Positions & Books
  const { data: posResp } = useQuery({
    queryKey: ['scalping', 'positions', appMode],
    queryFn: () => tradingApi.getPositions(apiKey ?? ''),
    enabled: !!apiKey,
    refetchOnWindowFocus: true,
  })
  const { data: ordResp } = useQuery({
    queryKey: ['scalping', 'orders', appMode],
    queryFn: () => tradingApi.getOrders(apiKey ?? ''),
    enabled: !!apiKey,
    refetchOnWindowFocus: true,
  })
  const { data: trdResp } = useQuery({
    queryKey: ['scalping', 'trades', appMode],
    queryFn: () => tradingApi.getTrades(apiKey ?? ''),
    enabled: !!apiKey,
    refetchOnWindowFocus: true,
  })
  const { data: trackedResp } = useQuery({
    queryKey: ['scalping', 'tracked', appMode],
    queryFn: () => scalpingApi.getTracked(),
  })

  const positions = useMemo(() => posResp?.data ?? [], [posResp])
  const orders = useMemo(() => ordResp?.data?.orders ?? [], [ordResp])
  const trades = useMemo(() => trdResp?.data ?? [], [trdResp])
  const trackedKeys = useMemo(
    () => new Set((trackedResp?.data ?? []).map((t) => `${t.exchange}:${t.symbol}:${t.product}`)),
    [trackedResp]
  )

  const scopedOrders = useMemo(
    () =>
      orders.filter(
        (o) =>
          isTodayTs(o.timestamp) &&
          trackedKeys.has(`${o.exchange}:${o.symbol}:${(o.product || '').toUpperCase()}`)
      ),
    [orders, trackedKeys]
  )
  const scopedTrades = useMemo(
    () =>
      trades.filter(
        (t) =>
          isTodayTs(t.timestamp) &&
          trackedKeys.has(`${t.exchange}:${t.symbol}:${(t.product || '').toUpperCase()}`)
      ),
    [trades, trackedKeys]
  )

  const lastRefreshRef = useRef(0)
  const refreshBooks = useCallback(() => {
    const now = performance.now()
    if (now - lastRefreshRef.current < REFRESH_THROTTLE_MS) return
    lastRefreshRef.current = now
    window.setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: ['scalping', 'positions'] })
      queryClient.invalidateQueries({ queryKey: ['scalping', 'orders'] })
      queryClient.invalidateQueries({ queryKey: ['scalping', 'trades'] })
      queryClient.invalidateQueries({ queryKey: ['scalping', 'tracked'] })
    }, 150)
  }, [queryClient])

  useOrderEventRefresh(refreshBooks, { events: [...BOOK_EVENTS], delay: 150 })

  // Subscriptions & Quotes
  const symbols = useMemo(() => {
    const seen = new Set<string>()
    const list: Array<{ symbol: string; exchange: string }> = []
    const add = (symbol: string, exchange: string) => {
      const k = `${exchange}:${symbol}`
      if (!symbol || !exchange || seen.has(k)) return
      seen.add(k)
      list.push({ symbol, exchange })
    }
    if (optionsMode && underlyingSym && underlyingExch) add(underlyingSym, underlyingExch)
    if (ceLeg) add(ceLeg.symbol, ceLeg.exchange)
    if (peLeg) add(peLeg.symbol, peLeg.exchange)
    if (singleLeg) add(singleLeg.symbol, singleLeg.exchange)
    for (const p of positions) add(p.symbol, p.exchange)
    for (const t of trades) add(t.symbol, t.exchange)
    return list
  }, [optionsMode, underlyingSym, underlyingExch, ceLeg, peLeg, singleLeg, positions, trades])

  const {
    data: marketData,
    isConnected,
    isAuthenticated,
    isFallbackMode,
  } = useMarketData({
    symbols,
    mode: 'Quote',
    enabled: symbols.length > 0,
  })

  const symbolsKey = useMemo(
    () => symbols.map((s) => `${s.exchange}:${s.symbol}`).join(','),
    [symbols]
  )
  const [mqMap, setMqMap] = useState<Map<string, QuotesData>>(new Map())
  useEffect(() => {
    if (!apiKey || symbols.length === 0) return
    let cancelled = false
    const fetchMq = () => {
      if (document.hidden) return
      tradingApi
        .getMultiQuotes(apiKey, symbols)
        .then((resp) => {
          if (cancelled || resp.status !== 'success' || !resp.results) return
          const next = new Map<string, QuotesData>()
          for (const r of resp.results) {
            if (r.data) next.set(`${r.exchange}:${r.symbol}`, r.data)
          }
          setMqMap(next)
        })
        .catch(() => {})
    }
    fetchMq()
    const id = window.setInterval(fetchMq, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [apiKey, symbolsKey])

  const getTick = useCallback(
    (symbol: string, exchange: string): TickView | undefined => {
      const key = `${exchange}:${symbol}`
      const entry = marketData.get(key)
      return mergeTick(
        entry?.data as TickView | undefined,
        entry?.lastUpdate,
        mqMap.get(key),
        Date.now(),
        TICK_STALE_MS
      )
    },
    [marketData, mqMap]
  )

  const ceTick = ceLeg ? getTick(ceLeg.symbol, ceLeg.exchange) : undefined
  const peTick = peLeg ? getTick(peLeg.symbol, peLeg.exchange) : undefined
  const underlyingTick = getTick(underlyingSym, underlyingExch)
  const singleTick = singleLeg ? getTick(singleLeg.symbol, singleLeg.exchange) : undefined

  const liveLtp = useCallback(
    (symbol: string, exchange: string): number | undefined => getTick(symbol, exchange)?.ltp,
    [getTick]
  )

  const marketDataRef = useRef(marketData)
  marketDataRef.current = marketData

  const { slMap, setSL, clearSL } = useTrailingSL(appMode)

  const positionRows = useMemo(
    () => buildPositionRows(positions, trades, slMap, liveLtp, trackedKeys),
    [positions, trades, slMap, liveLtp, trackedKeys]
  )

  const netQty = positionRows.reduce((a, r) => a + r.netQty, 0)
  const mtm = positionRows.reduce((a, r) => a + r.totalPnl, 0)

  // SL Dialog state
  const [slDialogTarget, setSlDialogTarget] = useState<SLTarget | null>(null)
  const slDialogOpen = slDialogTarget !== null
  const slDialogTick = slDialogTarget
    ? getTick(slDialogTarget.symbol, slDialogTarget.exchange)
    : undefined
  const slDialogPos = slDialogTarget
    ? positions.find(
        (p) =>
          p.symbol === slDialogTarget.symbol &&
          p.exchange === slDialogTarget.exchange &&
          p.product === slDialogTarget.product
      )
    : undefined
  const slDialogEntry = slDialogPos?.average_price ?? slDialogTick?.ltp ?? 0
  const slDialogQty = slDialogPos ? Math.abs(slDialogPos.quantity) : 0
  const slDialogSide: ScalpingAction = slDialogPos && slDialogPos.quantity < 0 ? 'SELL' : 'BUY'
  const slDialogLeg: SelectedLeg | null = slDialogTarget
    ? {
        symbol: slDialogTarget.symbol,
        exchange: slDialogTarget.exchange,
        optionType: slDialogTarget.optionType,
        strike: 0,
        lotsize: 0,
        tickSize: 0,
      }
    : null
  const slDialogExisting = slDialogTarget
    ? findLegSL(slMap, slDialogTarget.symbol, slDialogTarget.exchange, slDialogTarget.product)
    : undefined

  const openLegSL = (leg: SelectedLeg | null) => {
    if (leg) setSlDialogTarget({ ...leg, product })
  }

  const openRowSL = (row: ScalpingPositionRow) => {
    setSlDialogTarget({
      symbol: row.symbol,
      exchange: row.exchange,
      product: row.product,
      optionType: row.symbol.endsWith('PE') ? 'PE' : 'CE',
    })
  }

  const ceSL = ceLeg ? findLegSL(slMap, ceLeg.symbol, ceLeg.exchange, product) : undefined
  const peSL = peLeg ? findLegSL(slMap, peLeg.symbol, peLeg.exchange, product) : undefined

  // Orders logic
  const lastFireRef = useRef<number>(0)
  const predefRef = useRef({
    slOn: predefSlOn,
    slValue: predefSlValue,
    slUnit: predefSlUnit,
    tgtOn: predefTgtOn,
    tgtValue: predefTgtValue,
    tgtUnit: predefTgtUnit,
  })
  predefRef.current = {
    slOn: predefSlOn,
    slValue: predefSlValue,
    slUnit: predefSlUnit,
    tgtOn: predefTgtOn,
    tgtValue: predefTgtValue,
    tgtUnit: predefTgtUnit,
  }

  const stateRef = useRef({
    armed,
    lots,
    equityShares,
    segment,
    product,
    appMode,
    ceLeg,
    peLeg,
    singleLeg,
  })
  stateRef.current = {
    armed,
    lots,
    equityShares,
    segment,
    product,
    appMode,
    ceLeg,
    peLeg,
    singleLeg,
  }

  const attachPredefinedSL = useCallback(
    (leg: SelectedLeg, action: ScalpingAction, quantity: number, prod: ScalpingProduct) => {
      const cfg = predefRef.current
      if (!cfg.slOn && !cfg.tgtOn) return
      const ltp = marketDataRef.current.get(`${leg.exchange}:${leg.symbol}`)?.data?.ltp
      if (!ltp || ltp <= 0) return
      const toPts = (val: string, unit: 'PTS' | 'PCT') => {
        const n = Number(val)
        if (!Number.isFinite(n) || n <= 0) return 0
        return unit === 'PCT' ? (ltp * n) / 100 : n
      }
      const slPts = cfg.slOn ? toPts(cfg.slValue, cfg.slUnit) : 0
      const tgtPts = cfg.tgtOn ? toPts(cfg.tgtValue, cfg.tgtUnit) : 0
      if (slPts <= 0 && tgtPts <= 0) return
      const isBuy = action === 'BUY'
      const initialSl =
        slPts > 0 ? (isBuy ? ltp - slPts : ltp + slPts) : isBuy ? 0 : Number.MAX_SAFE_INTEGER
      const target = tgtPts > 0 ? (isBuy ? ltp + tgtPts : ltp - tgtPts) : 0
      setSL({
        symbol: leg.symbol,
        exchange: leg.exchange,
        product: prod,
        side: action,
        entry: ltp,
        quantity,
        initialSl,
        trailingEnabled: false,
        trailingStep: 0,
        highestPrice: ltp,
        lowestPrice: ltp,
        currentSl: initialSl,
        target,
        active: true,
      })
    },
    [setSL]
  )

  const submitOrder = useCallback(
    async (leg: SelectedLeg | null, action: ScalpingAction, sentLotsOverride?: number) => {
      const s = stateRef.current
      if (!s.armed) {
        showToast.error('One-Click is disarmed — enable it to trade', 'orders')
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

      const isEquity = s.segment === 'EQUITY'
      const sentLots = sentLotsOverride ?? (isEquity ? undefined : s.lots)
      const quantity = isEquity ? s.equityShares : (sentLots ?? s.lots) * leg.lotsize
      if (quantity <= 0) {
        showToast.error('Quantity must be positive', 'orders')
        return
      }

      const t0 = performance.now()
      const legLtp = marketDataRef.current.get(`${leg.exchange}:${leg.symbol}`)?.data?.ltp

      try {
        const res = await scalpingApi.placeOrder({
          symbol: leg.symbol,
          exchange: leg.exchange,
          action,
          quantity,
          product: s.product,
          lots: sentLots,
          ltp: legLtp != null && legLtp > 0 ? legLtp : undefined,
        })
        setLastLatencyMs(Math.round(performance.now() - t0))
        if (res.status === 'success') {
          attachPredefinedSL(leg, action, quantity, s.product)
        } else if (s.appMode === 'live') {
          showToast.error(res.message ?? 'Order failed', 'orders')
        }
      } catch (e) {
        setLastLatencyMs(Math.round(performance.now() - t0))
        const handledGlobally = s.appMode === 'analyzer' && !!(e as { response?: unknown }).response
        if (!handledGlobally) showToast.error(apiErrorMessage(e), 'orders')
      }
    },
    [attachPredefinedSL]
  )

  const doCloseAll = useCallback(async () => {
    try {
      const res = await scalpingApi.closeAll()
      if (res.status !== 'success' && appMode === 'live') {
        showToast.error(res.message ?? 'Close all failed', 'orders')
      }
    } catch (e) {
      const handledGlobally = appMode === 'analyzer' && !!(e as { response?: unknown }).response
      if (!handledGlobally) showToast.error(apiErrorMessage(e), 'orders')
    }
    refreshBooks()
  }, [refreshBooks, appMode])

  const doCancelAll = useCallback(async () => {
    try {
      const res = await scalpingApi.cancelAll()
      if (res.status !== 'success' && appMode === 'live') {
        showToast.error(res.message ?? 'Cancel all failed', 'orders')
      }
    } catch (e) {
      const handledGlobally = appMode === 'analyzer' && !!(e as { response?: unknown }).response
      if (!handledGlobally) showToast.error(apiErrorMessage(e), 'orders')
    }
    refreshBooks()
  }, [refreshBooks, appMode])

  const doCloseRow = useCallback(
    async (row: ScalpingPositionRow) => {
      if (row.netQty === 0) return
      const action: ScalpingAction = row.netQty > 0 ? 'SELL' : 'BUY'
      try {
        const res = await scalpingApi.closeLeg({
          symbol: row.symbol,
          exchange: row.exchange,
          action,
          quantity: Math.abs(row.netQty),
          product: row.product,
        })
        if (res.status !== 'success' && appMode === 'live') {
          showToast.error(res.message ?? 'Close failed', 'orders')
        }
      } catch (e) {
        const handledGlobally = appMode === 'analyzer' && !!(e as { response?: unknown }).response
        if (!handledGlobally) showToast.error(apiErrorMessage(e), 'orders')
      }
      refreshBooks()
    },
    [refreshBooks, appMode]
  )

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.repeat) return
      if (e.key === 'F6') {
        e.preventDefault()
        doCloseAll()
        return
      }
      if (e.key === 'F7') {
        e.preventDefault()
        doCancelAll()
        return
      }
      const t = e.target as HTMLElement | null
      if (
        t &&
        (t.tagName === 'INPUT' ||
          t.tagName === 'TEXTAREA' ||
          t.isContentEditable ||
          t.getAttribute('role') === 'combobox')
      ) {
        return
      }
      const s = stateRef.current
      if (s.segment !== 'OPTIONS') {
        switch (e.key) {
          case 'ArrowUp':
          case 'ArrowRight':
            e.preventDefault()
            submitOrder(s.singleLeg, 'BUY')
            return
          case 'ArrowDown':
          case 'ArrowLeft':
            e.preventDefault()
            submitOrder(s.singleLeg, 'SELL')
            return
          default:
            return
        }
      }
      switch (e.key) {
        case 'ArrowUp':
          e.preventDefault()
          submitOrder(s.ceLeg, 'BUY')
          break
        case 'ArrowDown':
          e.preventDefault()
          submitOrder(s.ceLeg, 'SELL')
          break
        case 'ArrowRight':
          e.preventDefault()
          submitOrder(s.peLeg, 'BUY')
          break
        case 'ArrowLeft':
          e.preventDefault()
          submitOrder(s.peLeg, 'SELL')
          break
        default:
          break
      }
    },
    [submitOrder, doCloseAll, doCancelAll]
  )

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  const wsBadge = isFallbackMode
    ? { label: 'Polling (REST)', variant: 'secondary' as const }
    : isAuthenticated
      ? { label: 'Live', variant: 'default' as const }
      : isConnected
        ? { label: 'Connecting…', variant: 'secondary' as const }
        : { label: 'Disconnected', variant: 'destructive' as const }

  const netQtyFor = useCallback(
    (sym?: string) => {
      if (!sym) return 0
      const r = positionRows.find((p) => p.symbol === sym)
      return r ? r.netQty : 0
    },
    [positionRows]
  )

  return (
    <div className="h-screen w-full flex flex-col bg-background overflow-hidden select-none">
      <Navbar />

      {/* Sync / Reconnecting Banner */}
      {syncBanner ? (
        <div className="bg-sky-500/15 text-sky-600 dark:text-sky-400 border-b border-sky-500/30 px-3 py-1 text-xs shrink-0 flex items-center justify-between font-medium">
          <span>⚡ {syncBanner}</span>
          <button type="button" onClick={() => setSyncBanner(null)} className="hover:opacity-80">
            ×
          </button>
        </div>
      ) : !isAuthenticated ? (
        <div className="bg-destructive/15 text-destructive border-b border-destructive/30 px-3 py-1 text-xs shrink-0 flex items-center justify-between">
          <span>{isFallbackMode ? 'Feed lost — using REST polling.' : 'Market-data feed disconnected — reconnecting…'}</span>
        </div>
      ) : null}

      {/* ── Compact Control Ribbon ─────────────────────────────────── */}
      <div className="shrink-0 border-b border-border/70 bg-card/70 px-3 py-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
        <div className="flex items-center gap-1.5">
          <span className="font-bold text-sm tracking-tight text-foreground flex items-center gap-1">
            <Activity className="h-4 w-4 text-primary" /> Scalper
          </span>
          <Badge
            variant={armed ? 'destructive' : 'secondary'}
            className="cursor-pointer text-[10px] px-1.5 py-0"
            onClick={() => setArmed(!armed)}
          >
            {armed ? 'ARMED' : 'ARM OFF'}
          </Badge>
          <Badge variant={wsBadge.variant} className="text-[10px] px-1.5 py-0">
            {wsBadge.label}
          </Badge>
          {lastLatencyMs != null && (
            <span className="font-mono text-[10px] text-muted-foreground">{lastLatencyMs}ms</span>
          )}
        </div>

        <div className="h-4 w-px bg-border/60" />

        {/* Exchange */}
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

        {/* Segment */}
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-muted-foreground">Seg</span>
          <Select value={segment} onValueChange={(v) => setSegment(v as Segment)}>
            <SelectTrigger className="h-7 w-20 text-xs px-1.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {isEquityExch ? (
                <SelectItem value="EQUITY" className="text-xs">Equity</SelectItem>
              ) : (
                <>
                  <SelectItem value="OPTIONS" className="text-xs">Options</SelectItem>
                  <SelectItem value="FUTURES" className="text-xs">Futures</SelectItem>
                </>
              )}
            </SelectContent>
          </Select>
        </div>

        {/* Underlying / Symbol */}
        {isEquityExch ? (
          <div className="relative flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Stock</span>
            <Input
              value={instrument ? instrument.symbol : searchQuery}
              placeholder="Search..."
              className="h-7 w-28 text-xs font-mono px-2"
              onChange={(e) => {
                setInstrument(null)
                setSearchQuery(e.target.value)
              }}
            />
            {!instrument && equityResults.length > 0 && (
              <div className="absolute top-8 left-10 z-50 max-h-48 w-44 overflow-auto rounded border bg-popover shadow-lg text-xs">
                {equityResults.slice(0, 15).map((r) => (
                  <button
                    type="button"
                    key={`${r.exchange}:${r.symbol}`}
                    className="block w-full px-2 py-1 text-left font-mono hover:bg-muted"
                    onClick={() => {
                      setInstrument(r)
                      setSearchQuery('')
                    }}
                  >
                    {r.symbol}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="relative flex items-center gap-1">
            <span className="text-[11px] text-muted-foreground">Und</span>
            <Input
              value={underlyingOpen ? underlyingQuery : underlying}
              placeholder="Underlying"
              className="h-7 w-24 text-xs font-mono font-bold px-2"
              onFocus={() => {
                setUnderlyingQuery('')
                setUnderlyingOpen(true)
              }}
              onChange={(e) => setUnderlyingQuery(e.target.value)}
              onBlur={() => window.setTimeout(() => setUnderlyingOpen(false), 180)}
            />
            {underlyingOpen && underlyingMatches.length > 0 && (
              <div className="absolute top-8 left-8 z-50 max-h-56 w-36 overflow-auto rounded border bg-popover shadow-lg text-xs">
                {underlyingMatches.map((nm) => (
                  <button
                    type="button"
                    key={nm}
                    className={`block w-full px-2 py-1 text-left font-mono hover:bg-muted ${nm === underlying ? 'bg-muted' : ''}`}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      setUnderlying(nm)
                      setUnderlyingQuery('')
                      setUnderlyingOpen(false)
                      setExpiry('')
                      setCeStrike('')
                      setPeStrike('')
                    }}
                  >
                    {nm}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Expiry */}
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

        {/* Lots / Shares */}
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-muted-foreground">
            {segment === 'EQUITY' ? 'Qty' : 'Lots'}
          </span>
          {segment === 'EQUITY' ? (
            <Input
              type="number"
              min={1}
              value={equityShares}
              onChange={(e) => setEquityShares(Math.max(1, Number(e.target.value) || 1))}
              className="h-7 w-16 text-center font-mono text-xs px-1"
            />
          ) : (
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
          )}
        </div>

        {/* Product */}
        <Select value={product} onValueChange={(v) => setProduct(v as ScalpingProduct)}>
          <SelectTrigger className="h-7 w-16 text-xs px-1.5">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="MIS" className="text-xs">MIS</SelectItem>
            {segment !== 'EQUITY' && <SelectItem value="NRML" className="text-xs">NRML</SelectItem>}
            {segment === 'EQUITY' && <SelectItem value="CNC" className="text-xs">CNC</SelectItem>}
          </SelectContent>
        </Select>

        <div className="h-4 w-px bg-border/60" />

        {/* Auto SL & Target */}
        <div className="flex items-center gap-1.5" title="Auto Stop-Loss on Entry">
          <Checkbox checked={predefSlOn} onCheckedChange={(v) => setPredefSlOn(v === true)} />
          <span className="text-[11px] text-muted-foreground">SL</span>
          <Input
            type="number"
            disabled={!predefSlOn}
            value={predefSlValue}
            onChange={(e) => setPredefSlValue(e.target.value)}
            placeholder="pts"
            className="h-7 w-12 text-center text-xs font-mono px-1"
          />
          <button
            type="button"
            disabled={!predefSlOn}
            onClick={() => setPredefSlUnit((u) => (u === 'PTS' ? 'PCT' : 'PTS'))}
            className="text-[10px] font-mono text-muted-foreground hover:text-foreground"
          >
            {predefSlUnit}
          </button>
        </div>

        <div className="flex items-center gap-1.5" title="Auto Target on Entry">
          <Checkbox checked={predefTgtOn} onCheckedChange={(v) => setPredefTgtOn(v === true)} />
          <span className="text-[11px] text-muted-foreground">Tgt</span>
          <Input
            type="number"
            disabled={!predefTgtOn}
            value={predefTgtValue}
            onChange={(e) => setPredefTgtValue(e.target.value)}
            placeholder="pts"
            className="h-7 w-12 text-center text-xs font-mono px-1"
          />
          <button
            type="button"
            disabled={!predefTgtOn}
            onClick={() => setPredefTgtUnit((u) => (u === 'PTS' ? 'PCT' : 'PTS'))}
            className="text-[10px] font-mono text-muted-foreground hover:text-foreground"
          >
            {predefTgtUnit}
          </button>
        </div>

        {/* Right side: Watchlist, Advisor, Charts, Actions */}
        <div className="ml-auto flex items-center gap-2">
          {/* Watchlist Quick Picker */}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 px-2 text-xs gap-1 border-border/80">
                <Bookmark className="h-3.5 w-3.5 text-amber-500" />
                <span>Watchlist</span>
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-64 p-2 text-xs">
              <div className="font-semibold mb-1 pb-1 border-b text-[11px] flex justify-between items-center">
                <span>Select from Watchlist</span>
                <span className="text-[10px] text-muted-foreground">{watchlists.length} lists</span>
              </div>
              <div className="max-h-60 overflow-y-auto space-y-1">
                {watchlists.flatMap((w) => w.items ?? []).length === 0 ? (
                  <div className="text-center py-3 text-muted-foreground text-[11px]">No watchlist items</div>
                ) : (
                  watchlists.map((w) => (
                    <div key={w.id} className="space-y-0.5">
                      <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider px-1 pt-1">
                        {w.name}
                      </div>
                      {(w.items ?? []).map((item) => (
                        <button
                          type="button"
                          key={item.id}
                          className="w-full flex items-center justify-between px-1.5 py-1 rounded hover:bg-muted text-left font-mono text-xs"
                          onClick={() => {
                            setSyncSymbol(`${item.exchange}:${item.symbol}`)
                          }}
                        >
                          <span className="font-medium">{item.symbol}</span>
                          <span className="text-[10px] text-muted-foreground">{item.exchange}</span>
                        </button>
                      ))}
                    </div>
                  ))
                )}
              </div>
            </PopoverContent>
          </Popover>

          {/* Scalper Advisor Alerts Popover */}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 px-2 text-xs gap-1 border-border/80 relative">
                <Zap className="h-3.5 w-3.5 text-sky-500" />
                <span>Advisor</span>
                {activeAlerts.length > 0 && (
                  <span className="ml-0.5 rounded-full bg-sky-500 px-1 text-[9px] font-bold text-white leading-tight">
                    {activeAlerts.length}
                  </span>
                )}
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-72 p-2 text-xs">
              <div className="font-semibold mb-1 pb-1 border-b text-[11px] flex justify-between items-center">
                <span>Scalper Advisor Alerts</span>
                <span className="text-[10px] text-muted-foreground">{activeAlerts.length} active</span>
              </div>
              <div className="max-h-64 overflow-y-auto space-y-1">
                {activeAlerts.length === 0 ? (
                  <div className="text-center py-4 text-muted-foreground text-[11px]">No active advisor alerts</div>
                ) : (
                  activeAlerts.map((alert) => (
                    <button
                      type="button"
                      key={alert.id}
                      className="w-full flex flex-col p-1.5 rounded border border-border/50 hover:bg-muted text-left transition-colors"
                      onClick={() => {
                        setSyncTarget({
                          key: alert.key,
                          underlying: alert.key,
                          exchange: alert.market ? (alert.market === 'NSE' ? 'NFO' : alert.market === 'BSE' ? 'BFO' : alert.market) : 'NFO',
                          side: alert.side,
                          strike: alert.strike ?? 0,
                          source: 'alert',
                        })
                      }}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-bold text-xs text-foreground">{alert.key}</span>
                        <span
                          className={cn(
                            'px-1 py-0.2 rounded text-[9px] font-bold',
                            alert.side === 'CE' ? 'bg-emerald-500/15 text-emerald-600' : 'bg-rose-500/15 text-rose-600'
                          )}
                        >
                          BUY {alert.side} {alert.strike ? `@${alert.strike}` : ''}
                        </span>
                      </div>
                      <div className="text-[10px] text-muted-foreground flex justify-between mt-0.5">
                        <span>Premium: ₹{alert.current_premium ?? alert.entry_premium ?? '—'}</span>
                        <span className="text-sky-600 font-semibold">Click to sync</span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </PopoverContent>
          </Popover>

          {/* Charts Toggle */}
          <div className="flex items-center gap-1.5 pl-1">
            <span className="text-[11px] text-muted-foreground">Chart</span>
            <Switch checked={showCharts} onCheckedChange={setShowCharts} className="scale-75" />
            {showCharts && (
              <div className="inline-flex rounded border border-border/80 p-0.5 bg-muted/40">
                {CHART_TIMEFRAMES.map((tf) => (
                  <button
                    type="button"
                    key={tf}
                    onClick={() => setChartTf(tf)}
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                      chartTf === tf ? 'bg-primary text-primary-foreground font-bold' : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {tf}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="h-4 w-px bg-border/60" />

          {/* MTM & Net */}
          <div className="flex items-center gap-2 font-mono text-xs">
            <span className="text-muted-foreground">Net: <span className="font-bold text-foreground">{netQty}</span></span>
            <span className={cn('font-bold', mtm >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
              MTM: ₹{mtm.toFixed(2)}
            </span>
          </div>

          <Button
            variant="destructive"
            size="sm"
            onClick={doCloseAll}
            className="h-7 text-xs font-bold px-2"
            title="Flatten all scalping positions (F6)"
          >
            Close All / F6
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={doCancelAll}
            className="h-7 text-xs font-bold px-2"
            title="Cancel all pending orders (F7)"
          >
            Cancel / F7
          </Button>
        </div>
      </div>

      {/* ── Main Area: Resizable Columns & Bottom Dock ──────────────── */}
      <ResizablePanelGroup
        orientation="vertical"
        id="scalper-main-layout"
        defaultLayout={verticalLayout.defaultLayout}
        onLayoutChanged={verticalLayout.onLayoutChanged}
        className="flex-1 min-h-0 overflow-hidden"
      >
        {/* Upper Resizable Trading Section */}
        <ResizablePanel
          id="scalper-trading-panel"
          defaultSize="74%"
          minSize="30%"
          className="flex flex-col min-h-0 p-1.5 overflow-hidden"
        >
          {optionsMode ? (
            <ResizablePanelGroup
              orientation="horizontal"
              id="scalper-options-columns"
              defaultLayout={optionsColumnsLayout.defaultLayout}
              onLayoutChanged={optionsColumnsLayout.onLayoutChanged}
              className="flex-1 min-h-0 gap-0"
            >
              {/* Column 1: Call (CE) */}
              <ResizablePanel id="panel-ce" defaultSize="33%" minSize="20%" className="min-h-0 flex flex-col">
                <div className="flex flex-col h-full min-h-0 rounded-md border border-border/80 bg-card p-1.5 overflow-hidden mr-0.5">
                  {/* Header */}
                  <div className="flex items-center gap-1.5 shrink-0 pb-1 border-b border-border/50">
                    <span className="rounded bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 font-bold text-xs px-1.5 py-0.5">
                      CE
                    </span>
                    <Select value={ceStrike} onValueChange={setCeStrike} disabled={chain.length === 0}>
                      <SelectTrigger className="h-6 w-32 text-xs font-mono font-bold px-1.5">
                        <SelectValue placeholder="CE strike" />
                      </SelectTrigger>
                      <SelectContent className="max-h-60 text-xs">
                        {chain.map((r) => (
                          <SelectItem key={`ce-${r.strike}`} value={String(r.strike)} className="text-xs font-mono">
                            {r.strike} {r.ce.label ? `(${r.ce.label})` : ''}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <div className="flex items-baseline gap-1 ml-auto font-mono">
                      <span className="text-sm font-bold text-emerald-600 dark:text-emerald-400">
                        {ceTick?.ltp != null ? ceTick.ltp.toFixed(priceDecimals(ceLeg?.exchange ?? foExchange)) : '—'}
                      </span>
                      {ceTick?.change_percent != null && (
                        <span className={cn('text-[10px] font-semibold', ceTick.change_percent >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
                          {ceTick.change_percent >= 0 ? '+' : ''}
                          {ceTick.change_percent.toFixed(1)}%
                        </span>
                      )}
                    </div>
                    {netQtyFor(ceLeg?.symbol) !== 0 && (
                      <span className="rounded bg-primary/10 text-primary font-mono text-[10px] font-bold px-1">
                        {netQtyFor(ceLeg?.symbol)}
                      </span>
                    )}
                  </div>

                  {/* Chart & Market Depth (Vertically Resizable) */}
                  {showCharts ? (
                    <ResizablePanelGroup orientation="vertical" id="col-ce-inner" className="flex-1 min-h-0 my-1">
                      <ResizablePanel id="col-ce-chart" defaultSize="46%" minSize="20%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 rounded overflow-hidden border border-border/50 bg-background/50">
                          <ScalpChart
                            symbol={ceLeg?.symbol ?? ''}
                            exchange={ceLeg?.exchange ?? ''}
                            interval={chartTf}
                            title="Call (CE)"
                          />
                        </div>
                      </ResizablePanel>
                      <ResizableHandle withHandle orientation="horizontal" className="my-0.5" />
                      <ResizablePanel id="col-ce-depth" defaultSize="54%" minSize="25%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 flex flex-col overflow-hidden">
                          <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                            <span>Market Depth</span>
                            <span className="font-mono">{ceLeg?.symbol}</span>
                          </div>
                          <div className="flex-1 min-h-0 overflow-y-auto">
                            <DepthTable
                              apiKey={apiKey}
                              symbol={ceLeg?.symbol ?? ''}
                              exchange={ceLeg?.exchange ?? foExchange}
                              enabled={!!ceLeg}
                              className="h-full"
                            />
                          </div>
                        </div>
                      </ResizablePanel>
                    </ResizablePanelGroup>
                  ) : (
                    <div className="flex-1 min-h-[70px] my-1 flex flex-col overflow-hidden">
                      <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                        <span>Market Depth</span>
                        <span className="font-mono">{ceLeg?.symbol}</span>
                      </div>
                      <div className="flex-1 min-h-0 overflow-y-auto">
                        <DepthTable
                          apiKey={apiKey}
                          symbol={ceLeg?.symbol ?? ''}
                          exchange={ceLeg?.exchange ?? foExchange}
                          enabled={!!ceLeg}
                          className="h-full"
                        />
                      </div>
                    </div>
                  )}

                  {/* Actions */}
                  <div className="shrink-0 pt-1 border-t border-border/50 space-y-1">
                    <div className="grid grid-cols-2 gap-1.5">
                      <Button
                        size="sm"
                        className="h-8 bg-emerald-600 hover:bg-emerald-700 font-bold text-xs"
                        onClick={() => submitOrder(ceLeg, 'BUY')}
                      >
                        ↑ BUY CE
                      </Button>
                      <Button
                        size="sm"
                        className="h-8 bg-rose-600 hover:bg-rose-700 font-bold text-xs"
                        onClick={() => submitOrder(ceLeg, 'SELL')}
                      >
                        ↓ SELL CE
                      </Button>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 flex-1 text-[10px] font-medium"
                        onClick={() => openLegSL(ceLeg)}
                        disabled={!ceLeg}
                      >
                        {ceSL ? 'Edit SL' : 'Set SL'}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 flex-1 text-[10px] font-medium border-amber-500/40 text-amber-600 hover:bg-amber-500/10"
                        onClick={() => {
                          const row = positionRows.find((p) => p.symbol === ceLeg?.symbol)
                          if (row) doCloseRow(row)
                          else showToast.info('No open position for this contract')
                        }}
                        disabled={!ceLeg || netQtyFor(ceLeg?.symbol) === 0}
                      >
                        Square Off
                      </Button>
                    </div>
                  </div>
                </div>
              </ResizablePanel>

              {/* Horizontal Divider between CE and SPOT */}
              <ResizableHandle withHandle orientation="vertical" className="mx-0.5" />

              {/* Column 2: SPOT / Underlying */}
              <ResizablePanel id="panel-spot" defaultSize="34%" minSize="20%" className="min-h-0 flex flex-col">
                <div className="flex flex-col h-full min-h-0 rounded-md border border-border/80 bg-card p-1.5 overflow-hidden mx-0.5">
                  {/* Header */}
                  <div className="flex items-center gap-1.5 shrink-0 pb-1 border-b border-border/50">
                    <span className="rounded bg-sky-500/15 text-sky-600 dark:text-sky-400 font-bold text-xs px-1.5 py-0.5">
                      SPOT
                    </span>
                    <span className="font-bold text-xs font-mono">{underlying}</span>
                    <div className="flex items-baseline gap-1 ml-auto font-mono">
                      <span className="text-sm font-bold text-foreground">
                        {underlyingTick?.ltp != null ? underlyingTick.ltp.toFixed(priceDecimals(underlyingExch)) : '—'}
                      </span>
                      {underlyingTick?.change_percent != null && (
                        <span className={cn('text-[10px] font-semibold', underlyingTick.change_percent >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
                          {underlyingTick.change_percent >= 0 ? '+' : ''}
                          {underlyingTick.change_percent.toFixed(1)}%
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Chart & Market Depth (Vertically Resizable) */}
                  {showCharts ? (
                    <ResizablePanelGroup orientation="vertical" id="col-spot-inner" className="flex-1 min-h-0 my-1">
                      <ResizablePanel id="col-spot-chart" defaultSize="46%" minSize="20%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 rounded overflow-hidden border border-border/50 bg-background/50">
                          <ScalpChart
                            symbol={underlyingSym}
                            exchange={underlyingExch}
                            interval={chartTf}
                            title={underlying || 'Underlying'}
                          />
                        </div>
                      </ResizablePanel>
                      <ResizableHandle withHandle orientation="horizontal" className="my-0.5" />
                      <ResizablePanel id="col-spot-depth" defaultSize="54%" minSize="25%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 flex flex-col overflow-hidden">
                          <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                            <span>Underlying Depth</span>
                            <span className="font-mono">{underlyingSym}</span>
                          </div>
                          <div className="flex-1 min-h-0 overflow-y-auto">
                            <DepthTable
                              apiKey={apiKey}
                              symbol={underlyingSym}
                              exchange={underlyingExch}
                              enabled={!!underlyingSym}
                              className="h-full"
                            />
                          </div>
                        </div>
                      </ResizablePanel>
                    </ResizablePanelGroup>
                  ) : (
                    <div className="flex-1 min-h-[70px] my-1 flex flex-col overflow-hidden">
                      <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                        <span>Underlying Depth</span>
                        <span className="font-mono">{underlyingSym}</span>
                      </div>
                      <div className="flex-1 min-h-0 overflow-y-auto">
                        <DepthTable
                          apiKey={apiKey}
                          symbol={underlyingSym}
                          exchange={underlyingExch}
                          enabled={!!underlyingSym}
                          className="h-full"
                        />
                      </div>
                    </div>
                  )}

                  {/* Range & Info */}
                  <div className="shrink-0 pt-1 border-t border-border/50 space-y-1">
                    <RangeBarCompact
                      ltp={underlyingTick?.ltp}
                      open={underlyingTick?.open}
                      high={underlyingTick?.high}
                      low={underlyingTick?.low}
                      decimals={priceDecimals(underlyingExch)}
                    />
                    <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono bg-muted/20 px-1.5 py-1 rounded">
                      <span>ATM: <strong className="text-foreground">{chainResp?.atm_strike ?? '—'}</strong></span>
                      <span>Lotsize: <strong className="text-foreground">{chain[0]?.ce?.lotsize ?? '—'}</strong></span>
                      <span>Exp: <strong className="text-foreground">{expiry || '—'}</strong></span>
                    </div>
                  </div>
                </div>
              </ResizablePanel>

              {/* Horizontal Divider between SPOT and PE */}
              <ResizableHandle withHandle orientation="vertical" className="mx-0.5" />

              {/* Column 3: Put (PE) */}
              <ResizablePanel id="panel-pe" defaultSize="33%" minSize="20%" className="min-h-0 flex flex-col">
                <div className="flex flex-col h-full min-h-0 rounded-md border border-border/80 bg-card p-1.5 overflow-hidden ml-0.5">
                  {/* Header */}
                  <div className="flex items-center gap-1.5 shrink-0 pb-1 border-b border-border/50">
                    <span className="rounded bg-rose-500/15 text-rose-600 dark:text-rose-400 font-bold text-xs px-1.5 py-0.5">
                      PE
                    </span>
                    <Select value={peStrike} onValueChange={setPeStrike} disabled={chain.length === 0}>
                      <SelectTrigger className="h-6 w-32 text-xs font-mono font-bold px-1.5">
                        <SelectValue placeholder="PE strike" />
                      </SelectTrigger>
                      <SelectContent className="max-h-60 text-xs">
                        {chain.map((r) => (
                          <SelectItem key={`pe-${r.strike}`} value={String(r.strike)} className="text-xs font-mono">
                            {r.strike} {r.pe.label ? `(${r.pe.label})` : ''}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <div className="flex items-baseline gap-1 ml-auto font-mono">
                      <span className="text-sm font-bold text-rose-600 dark:text-rose-400">
                        {peTick?.ltp != null ? peTick.ltp.toFixed(priceDecimals(peLeg?.exchange ?? foExchange)) : '—'}
                      </span>
                      {peTick?.change_percent != null && (
                        <span className={cn('text-[10px] font-semibold', peTick.change_percent >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
                          {peTick.change_percent >= 0 ? '+' : ''}
                          {peTick.change_percent.toFixed(1)}%
                        </span>
                      )}
                    </div>
                    {netQtyFor(peLeg?.symbol) !== 0 && (
                      <span className="rounded bg-primary/10 text-primary font-mono text-[10px] font-bold px-1">
                        {netQtyFor(peLeg?.symbol)}
                      </span>
                    )}
                  </div>

                  {/* Chart & Market Depth (Vertically Resizable) */}
                  {showCharts ? (
                    <ResizablePanelGroup orientation="vertical" id="col-pe-inner" className="flex-1 min-h-0 my-1">
                      <ResizablePanel id="col-pe-chart" defaultSize="46%" minSize="20%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 rounded overflow-hidden border border-border/50 bg-background/50">
                          <ScalpChart
                            symbol={peLeg?.symbol ?? ''}
                            exchange={peLeg?.exchange ?? ''}
                            interval={chartTf}
                            title="Put (PE)"
                          />
                        </div>
                      </ResizablePanel>
                      <ResizableHandle withHandle orientation="horizontal" className="my-0.5" />
                      <ResizablePanel id="col-pe-depth" defaultSize="54%" minSize="25%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 flex flex-col overflow-hidden">
                          <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                            <span>Market Depth</span>
                            <span className="font-mono">{peLeg?.symbol}</span>
                          </div>
                          <div className="flex-1 min-h-0 overflow-y-auto">
                            <DepthTable
                              apiKey={apiKey}
                              symbol={peLeg?.symbol ?? ''}
                              exchange={peLeg?.exchange ?? foExchange}
                              enabled={!!peLeg}
                              className="h-full"
                            />
                          </div>
                        </div>
                      </ResizablePanel>
                    </ResizablePanelGroup>
                  ) : (
                    <div className="flex-1 min-h-[70px] my-1 flex flex-col overflow-hidden">
                      <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-0.5 px-0.5 flex justify-between shrink-0">
                        <span>Market Depth</span>
                        <span className="font-mono">{peLeg?.symbol}</span>
                      </div>
                      <div className="flex-1 min-h-0 overflow-y-auto">
                        <DepthTable
                          apiKey={apiKey}
                          symbol={peLeg?.symbol ?? ''}
                          exchange={peLeg?.exchange ?? foExchange}
                          enabled={!!peLeg}
                          className="h-full"
                        />
                      </div>
                    </div>
                  )}

                  {/* Actions */}
                  <div className="shrink-0 pt-1 border-t border-border/50 space-y-1">
                    <div className="grid grid-cols-2 gap-1.5">
                      <Button
                        size="sm"
                        className="h-8 bg-emerald-600 hover:bg-emerald-700 font-bold text-xs"
                        onClick={() => submitOrder(peLeg, 'BUY')}
                      >
                        → BUY PE
                      </Button>
                      <Button
                        size="sm"
                        className="h-8 bg-rose-600 hover:bg-rose-700 font-bold text-xs"
                        onClick={() => submitOrder(peLeg, 'SELL')}
                      >
                        ← SELL PE
                      </Button>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 flex-1 text-[10px] font-medium"
                        onClick={() => openLegSL(peLeg)}
                        disabled={!peLeg}
                      >
                        {peSL ? 'Edit SL' : 'Set SL'}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 flex-1 text-[10px] font-medium border-amber-500/40 text-amber-600 hover:bg-amber-500/10"
                        onClick={() => {
                          const row = positionRows.find((p) => p.symbol === peLeg?.symbol)
                          if (row) doCloseRow(row)
                          else showToast.info('No open position for this contract')
                        }}
                        disabled={!peLeg || netQtyFor(peLeg?.symbol) === 0}
                      >
                        Square Off
                      </Button>
                    </div>
                  </div>
                </div>
              </ResizablePanel>
            </ResizablePanelGroup>
          ) : (
            /* Single Instrument (Equity / Futures) */
            <ResizablePanelGroup
              orientation="horizontal"
              id="scalper-equity-columns"
              defaultLayout={equityColumnsLayout.defaultLayout}
              onLayoutChanged={equityColumnsLayout.onLayoutChanged}
              className="flex-1 min-h-0 gap-0"
            >
              <ResizablePanel id="panel-equity-main" defaultSize="60%" minSize="30%" className="min-h-0 flex flex-col">
                <div className="flex flex-col h-full min-h-0 rounded-md border border-border/80 bg-card p-2 overflow-hidden mr-0.5">
                  <div className="flex items-center gap-2 shrink-0 pb-1 border-b border-border/50">
                    <Badge variant="outline" className="font-mono">
                      {segment === 'EQUITY' ? 'EQUITY' : 'FUTURES'}
                    </Badge>
                    <span className="font-bold text-sm font-mono">{singleLeg?.symbol}</span>
                    <div className="flex items-baseline gap-1 ml-auto font-mono">
                      <span className="text-base font-bold text-foreground">
                        {singleTick?.ltp != null ? singleTick.ltp.toFixed(priceDecimals(singleLeg?.exchange)) : '—'}
                      </span>
                      {singleTick?.change_percent != null && (
                        <span className={cn('text-xs font-semibold', singleTick.change_percent >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
                          {singleTick.change_percent >= 0 ? '+' : ''}
                          {singleTick.change_percent.toFixed(1)}%
                        </span>
                      )}
                    </div>
                  </div>

                  {showCharts ? (
                    <ResizablePanelGroup orientation="vertical" id="col-eq-inner" className="flex-1 min-h-0 my-1">
                      <ResizablePanel id="col-eq-chart" defaultSize="50%" minSize="20%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 rounded overflow-hidden border border-border/50">
                          <ScalpChart
                            symbol={singleLeg?.symbol ?? ''}
                            exchange={singleLeg?.exchange ?? ''}
                            interval={chartTf}
                            title={segment === 'EQUITY' ? 'Equity' : 'Futures'}
                          />
                        </div>
                      </ResizablePanel>
                      <ResizableHandle withHandle orientation="horizontal" className="my-0.5" />
                      <ResizablePanel id="col-eq-depth" defaultSize="50%" minSize="25%" className="min-h-0 flex flex-col">
                        <div className="h-full min-h-0 flex flex-col overflow-hidden">
                          <span className="text-[10px] font-semibold text-muted-foreground uppercase mb-0.5 shrink-0">Market Depth</span>
                          <div className="flex-1 min-h-0 overflow-y-auto">
                            <DepthTable apiKey={apiKey} symbol={singleLeg?.symbol ?? ''} exchange={singleLeg?.exchange ?? ''} enabled={!!singleLeg} className="h-full" />
                          </div>
                        </div>
                      </ResizablePanel>
                    </ResizablePanelGroup>
                  ) : (
                    <div className="flex-1 min-h-[90px] my-1 flex flex-col overflow-hidden">
                      <span className="text-[10px] font-semibold text-muted-foreground uppercase mb-0.5 shrink-0">Market Depth</span>
                      <div className="flex-1 min-h-0 overflow-y-auto">
                        <DepthTable apiKey={apiKey} symbol={singleLeg?.symbol ?? ''} exchange={singleLeg?.exchange ?? ''} enabled={!!singleLeg} className="h-full" />
                      </div>
                    </div>
                  )}

                  <div className="shrink-0 pt-2 border-t border-border/50 space-y-1.5">
                    <div className="grid grid-cols-2 gap-2">
                      <Button
                        size="sm"
                        className="h-9 bg-emerald-600 hover:bg-emerald-700 font-bold"
                        onClick={() => submitOrder(singleLeg, 'BUY')}
                      >
                        ↑ BUY
                      </Button>
                      <Button
                        size="sm"
                        className="h-9 bg-rose-600 hover:bg-rose-700 font-bold"
                        onClick={() => submitOrder(singleLeg, 'SELL')}
                      >
                        ↓ SELL
                      </Button>
                    </div>
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 flex-1 text-xs"
                        onClick={() => openLegSL(singleLeg)}
                        disabled={!singleLeg}
                      >
                        {singleLeg && findLegSL(slMap, singleLeg.symbol, singleLeg.exchange, product) ? 'Edit SL' : 'Set SL'}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 flex-1 text-xs border-amber-500/40 text-amber-600"
                        onClick={() => {
                          const row = positionRows.find((p) => p.symbol === singleLeg?.symbol)
                          if (row) doCloseRow(row)
                          else showToast.info('No open position')
                        }}
                        disabled={!singleLeg || netQtyFor(singleLeg?.symbol) === 0}
                      >
                        Square Off
                      </Button>
                    </div>
                  </div>
                </div>
              </ResizablePanel>

              {/* Horizontal Divider between Equity Main and Stats */}
              <ResizableHandle withHandle orientation="vertical" className="mx-0.5" />

              <ResizablePanel id="panel-equity-stats" defaultSize="40%" minSize="20%" className="min-h-0 flex flex-col">
                <div className="flex flex-col h-full min-h-0 rounded-md border border-border/80 bg-card p-2 overflow-hidden ml-0.5">
                  <div className="font-semibold text-xs border-b border-border/50 pb-1">Benchmark &amp; Day Range</div>
                  <div className="my-2">
                    <RangeBarCompact
                      ltp={singleTick?.ltp}
                      open={singleTick?.open}
                      high={singleTick?.high}
                      low={singleTick?.low}
                      decimals={priceDecimals(singleLeg?.exchange)}
                    />
                  </div>
                  <div className="text-xs text-muted-foreground p-3 rounded bg-muted/20 space-y-2 mt-auto">
                    <p>• Arrow Keys: <strong>↑ / → Buy</strong> · <strong>↓ / ← Sell</strong></p>
                    <p>• Flatten: <strong>F6 Close All Positions</strong></p>
                    <p>• Cancel: <strong>F7 Cancel All Pending Orders</strong></p>
                  </div>
                </div>
              </ResizablePanel>
            </ResizablePanelGroup>
          )}
        </ResizablePanel>

        {/* Horizontal Drag Handle between Trading Columns and Bottom Dock */}
        <ResizableHandle withHandle orientation="horizontal" className="my-0.5" />

        {/* ── Bottom Books Dock (Positions · Orders · Trades) ────────── */}
        <ResizablePanel
          id="scalper-dock-panel"
          defaultSize="26%"
          minSize="10%"
          className="flex flex-col min-h-0 bg-card/70 border-t border-border/80"
        >
          <Tabs value={bookTab} onValueChange={(v) => setBookTab(v as 'positions' | 'orders' | 'trades')} className="flex-1 flex flex-col min-h-0">
          <div className="flex items-center justify-between px-2 pt-1 border-b border-border/40 shrink-0">
            <TabsList className="h-7 bg-muted/60 p-0.5">
              <TabsTrigger value="positions" className="text-xs h-6 px-2.5">
                Positions ({positionRows.length})
              </TabsTrigger>
              <TabsTrigger value="orders" className="text-xs h-6 px-2.5">
                Orders ({scopedOrders.length})
              </TabsTrigger>
              <TabsTrigger value="trades" className="text-xs h-6 px-2.5">
                Trades ({scopedTrades.length})
              </TabsTrigger>
            </TabsList>
            <div className="text-[10px] text-muted-foreground font-mono">
              Keys: ↑ Buy CE · ↓ Sell CE · → Buy PE · ← Sell PE · F6 Close · F7 Cancel
            </div>
          </div>

          <TabsContent value="positions" className="flex-1 min-h-0 m-0 overflow-y-auto">
            {positionRows.length === 0 ? (
              <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
                No active scalping positions
              </div>
            ) : (
              <Table className="text-xs">
                <TableHeader className="sticky top-0 bg-card z-10">
                  <TableRow className="h-6">
                    <TableHead className="py-1">Symbol</TableHead>
                    <TableHead className="py-1">Prod</TableHead>
                    <TableHead className="py-1">Side</TableHead>
                    <TableHead className="py-1 text-right">Net Qty</TableHead>
                    <TableHead className="py-1 text-right">LTP</TableHead>
                    <TableHead className="py-1 text-right">SL</TableHead>
                    <TableHead className="py-1 text-right">TP</TableHead>
                    <TableHead className="py-1 text-right">TSL</TableHead>
                    <TableHead className="py-1 text-right">Total P&amp;L</TableHead>
                    <TableHead className="py-1">SL</TableHead>
                    <TableHead className="py-1 text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="font-mono text-xs">
                  {positionRows.map((r) => {
                    const open = r.netQty !== 0
                    const dec = priceDecimals(r.exchange)
                    return (
                      <TableRow key={`${r.exchange}:${r.symbol}:${r.product}`} className="h-7">
                        <TableCell className="py-1 font-semibold">{r.symbol}</TableCell>
                        <TableCell className="py-1">{r.product}</TableCell>
                        <TableCell className={r.side === 'BUY' ? 'text-emerald-600 font-bold' : r.side === 'SELL' ? 'text-rose-600 font-bold' : 'text-muted-foreground'}>
                          {r.side}
                        </TableCell>
                        <TableCell className="py-1 text-right font-bold">{r.netQty}</TableCell>
                        <TableCell className="py-1 text-right">{r.ltp ? r.ltp.toFixed(dec) : '—'}</TableCell>
                        <TableCell className="py-1 text-right">{r.sl != null ? r.sl.toFixed(dec) : '-'}</TableCell>
                        <TableCell className="py-1 text-right">{r.target != null ? r.target.toFixed(dec) : '-'}</TableCell>
                        <TableCell className="py-1 text-right">{r.trailingStep != null ? `±${r.trailingStep}` : '-'}</TableCell>
                        <TableCell className={cn('py-1 text-right font-bold', r.totalPnl >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
                          {r.totalPnl.toFixed(2)}
                        </TableCell>
                        <TableCell className="py-1">
                          {open ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-5 text-[10px] px-1 font-sans"
                              onClick={() => openRowSL(r)}
                            >
                              {r.sl != null || r.target != null ? 'Edit SL' : '+ SL'}
                            </Button>
                          ) : (
                            '-'
                          )}
                        </TableCell>
                        <TableCell className="py-1 text-right">
                          {open && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-5 text-[10px] text-destructive px-1 hover:bg-destructive/10 font-sans"
                              onClick={() => doCloseRow(r)}
                            >
                              Close
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            )}
          </TabsContent>

          <TabsContent value="orders" className="flex-1 min-h-0 m-0 overflow-y-auto">
            {scopedOrders.length === 0 ? (
              <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
                No orders today
              </div>
            ) : (
              <Table className="text-xs">
                <TableHeader className="sticky top-0 bg-card z-10">
                  <TableRow className="h-6">
                    <TableHead className="py-1">Time</TableHead>
                    <TableHead className="py-1">Symbol</TableHead>
                    <TableHead className="py-1">Action</TableHead>
                    <TableHead className="py-1 text-right">Qty</TableHead>
                    <TableHead className="py-1 text-right">Price</TableHead>
                    <TableHead className="py-1">Status</TableHead>
                    <TableHead className="py-1 font-mono">Order ID</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="font-mono text-xs">
                  {scopedOrders.map((ord) => (
                    <TableRow key={ord.orderid} className="h-7">
                      <TableCell className="py-1">{ord.timestamp ? ord.timestamp.slice(11, 19) : '—'}</TableCell>
                      <TableCell className="py-1 font-semibold">{ord.symbol}</TableCell>
                      <TableCell className="py-1">
                        <span className={ord.action === 'BUY' ? 'text-emerald-600 font-bold' : 'text-rose-600 font-bold'}>
                          {ord.action}
                        </span>
                      </TableCell>
                      <TableCell className="py-1 text-right">{ord.quantity}</TableCell>
                      <TableCell className="py-1 text-right">{ord.price ?? ord.pricetype ?? 'MKT'}</TableCell>
                      <TableCell className="py-1">
                        <Badge variant="outline" className="text-[9px] px-1 py-0">{ord.order_status}</Badge>
                      </TableCell>
                      <TableCell className="py-1 text-muted-foreground text-[10px]">{ord.orderid}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>

          <TabsContent value="trades" className="flex-1 min-h-0 m-0 overflow-y-auto">
            {scopedTrades.length === 0 ? (
              <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
                No trades today
              </div>
            ) : (
              <Table className="text-xs">
                <TableHeader className="sticky top-0 bg-card z-10">
                  <TableRow className="h-6">
                    <TableHead className="py-1">Time</TableHead>
                    <TableHead className="py-1">Symbol</TableHead>
                    <TableHead className="py-1">Action</TableHead>
                    <TableHead className="py-1 text-right">Qty</TableHead>
                    <TableHead className="py-1 text-right">Avg Price</TableHead>
                    <TableHead className="py-1 font-mono">Order ID</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="font-mono text-xs">
                  {scopedTrades.map((trd) => (
                    <TableRow key={`${trd.orderid}-${trd.timestamp}`} className="h-7">
                      <TableCell className="py-1">{trd.timestamp ? trd.timestamp.slice(11, 19) : '—'}</TableCell>
                      <TableCell className="py-1 font-semibold">{trd.symbol}</TableCell>
                      <TableCell className="py-1">
                        <span className={trd.action === 'BUY' ? 'text-emerald-600 font-bold' : 'text-rose-600 font-bold'}>
                          {trd.action}
                        </span>
                      </TableCell>
                      <TableCell className="py-1 text-right">{trd.quantity}</TableCell>
                      <TableCell className="py-1 text-right">
                        {Number(trd.average_price || 0).toFixed(priceDecimals(trd.exchange))}
                      </TableCell>
                      <TableCell className="py-1 text-muted-foreground text-[10px]">{trd.orderid}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>
        </Tabs>
      </ResizablePanel>
    </ResizablePanelGroup>

      {/* Set SL Dialog */}
      <SetSLDialog
        open={slDialogOpen}
        onOpenChange={(o) => {
          if (!o) setSlDialogTarget(null)
        }}
        leg={slDialogLeg}
        product={slDialogTarget?.product ?? product}
        side={slDialogSide}
        entryPrice={slDialogEntry}
        quantity={slDialogQty}
        ltp={slDialogTick?.ltp}
        existing={slDialogExisting}
        onSave={(sl) => {
          setSL(sl)
          setSlDialogTarget(null)
        }}
        onClear={() => {
          if (slDialogTarget) {
            clearSL(slDialogTarget.symbol, slDialogTarget.exchange, slDialogTarget.product)
          }
          setSlDialogTarget(null)
        }}
      />
    </div>
  )
}
