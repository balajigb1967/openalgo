import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { tradingApi, type QuotesData, type DepthData } from '@/api/trading'
import { scalpingApi as termApi } from '@/api/scalping'
import { type ScalperTarget, subscribeSync, subscribeSyncTarget } from '@/lib/scalperSync'
import { priceDecimals } from '@/lib/scalpingPrice'
import { mergeTick, type TickView } from '@/lib/scalpingTick'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ScalperChart } from '@/components/scalping/ScalperChart'
import { useMarketData } from '@/hooks/useMarketData'
import type {
  ScalpingAction,
  ScalpingProduct,
  ScalpingOrderRequest,
  SelectedLeg,
  Segment,
} from '@/types/scalping'
import { showToast } from '@/utils/toast'
import { X } from 'lucide-react'

/**
 * Scalper terminal, fno-trader-pro style: three columns — CE · SPOT · PE.
 *
 * Every column carries a live LTP badge, a candle chart (shared MarketData
 * feed + broker history via ScalpChart), a 5-level REST depth table with
 * quantity bars, and its own order controls (qty / type / product / price
 * plus BUY · SELL · SQUARE) — the layout the user's original scalper widget
 * had. Columns resize together via the drag bar under each chart.
 *
 * Sync: follows the focused chart pane's symbol and the advisor alerts
 * (lib/scalperSync) exactly as before.
 */

const ORDER_COOLDOWN_MS = 120
const CHART_H_KEY = 'oa-scalper-term-chart-h'

type TermExchange = 'NFO' | 'BFO' | 'MCX' | 'CDS'
const EXCHANGES: TermExchange[] = ['NFO', 'BFO', 'MCX', 'CDS']
const DEFAULT_UNDERLYING: Record<TermExchange, string> = {
  NFO: 'NIFTY',
  BFO: 'SENSEX',
  MCX: 'CRUDEOIL',
  CDS: 'USDINR',
}
function exchForMarket(m: string | null | undefined): TermExchange {
  return ({ NSE: 'NFO', BSE: 'BFO', MCX: 'MCX', CDS: 'CDS' } as Record<string, TermExchange>)[
    (m || '').toUpperCase()
  ] ?? 'NFO'
}

function exchForChartPrefix(p: string): TermExchange {
  return (
    ({
      NSE: 'NFO',
      NFO: 'NFO',
      NSE_INDEX: 'NFO',
      BSE: 'BFO',
      BFO: 'BFO',
      BSE_INDEX: 'BFO',
      MCX: 'MCX',
      MCX_INDEX: 'MCX',
      CDS: 'CDS',
    } as Record<string, TermExchange>)[p.toUpperCase()] ?? 'NFO'
  )
}

/** Index display names → their F&O option-family root. */
const INDEX_ROOT_MAP: Record<string, string> = {
  'NIFTY 50': 'NIFTY',
  NIFTY50: 'NIFTY',
  'NIFTY BANK': 'BANKNIFTY',
  'FIN NIFTY': 'FINNIFTY',
  'NIFTY FIN SERVICE': 'FINNIFTY',
  'NIFTY FINANCIAL SERVICES': 'FINNIFTY',
  'NIFTY MIDCAP SELECT': 'MIDCPNIFTY',
  SENSEX: 'SENSEX',
  'BSE SENSEX': 'SENSEX',
  'BSE SENSEX 50': 'SENSEX50',
}

/** F&O root for a clicked symbol: index display names mapped, noise stripped. */
function normalizeUnd(sym: string): string {
  const s = sym.trim().toUpperCase()
  return INDEX_ROOT_MAP[s] ?? cleanRoot(s)
}

function cleanRoot(s: string): string {
  return s
    .replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d*/g, '')
    .replace(/\d+$/, '')
}

function underlyingFromChart(symbol: string): {
  underlying: string
  rawSym: string
  isOption: boolean
  optionType: 'CE' | 'PE' | null
  strike: number | null
} {
  const sym = symbol.split(':')[1] ?? symbol
  const upper = sym.toUpperCase()
  const m = upper.match(/^(.+?)(\d+(?:\.\d+)?)(CE|PE)$/)
  if (m) {
    return {
      underlying: normalizeUnd(m[1]),
      rawSym: sym,
      isOption: true,
      optionType: m[3] as 'CE' | 'PE',
      strike: Number(m[2]),
    }
  }
  return {
    underlying: normalizeUnd(upper.replace(/FUT$/, '')),
    rawSym: sym,
    isOption: false,
    optionType: null,
    strike: null,
  }
}

interface Props {
  apiKey: string
  wsUrl: string
  armed: boolean
  onClose: () => void
}

export function ScalperTerminal({ apiKey, wsUrl, armed, onClose }: Props) {
  const queryClient = useQueryClient()

  /* ── selection state ─────────────────────────────────────────────────── */
  const [exchange, setExchange] = useState<TermExchange>('NFO')
  const [segment, setSegment] = useState<Segment>('OPTIONS')
  const [underlying, setUnderlying] = useState('NIFTY')
  const [expiry, setExpiry] = useState('')
  const [ceStrike, setCeStrike] = useState('')
  const [peStrike, setPeStrike] = useState('')
  const [strikeCount, setStrikeCount] = useState(8)
  const [followChart, setFollowChart] = useState(true)

  /* ── layout state ────────────────────────────────────────────────────── */
  const [chartH, setChartH] = useState<number>(() => {
    const saved = Number(localStorage.getItem(CHART_H_KEY))
    return saved >= 60 && saved <= 400 ? saved : 150
  })
  useEffect(() => {
    try {
      localStorage.setItem(CHART_H_KEY, String(chartH))
    } catch {
      /* storage refused */
    }
  }, [chartH])
  const dragRef = useRef<{ startY: number; startH: number } | null>(null)
  const onDragStart = useCallback(
    (e: React.MouseEvent | React.TouchEvent) => {
      e.preventDefault()
      const y = 'touches' in e ? e.touches[0].clientY : e.clientY
      dragRef.current = { startY: y, startH: chartH }
      const move = (ev: MouseEvent | TouchEvent) => {
        if (!dragRef.current) return
        const cy = 'touches' in ev ? ev.touches[0].clientY : ev.clientY
        setChartH(Math.max(60, Math.min(400, dragRef.current.startH + (cy - dragRef.current.startY))))
      }
      const up = () => {
        dragRef.current = null
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        window.removeEventListener('touchmove', move)
        window.removeEventListener('touchend', up)
      }
      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
      window.addEventListener('touchmove', move, { passive: false })
      window.addEventListener('touchend', up)
    },
    [chartH]
  )

  /* ── per-column order state ──────────────────────────────────────────── */
  const [ordCfg, setOrdCfg] = useState({
    product: 'MIS' as ScalpingProduct,
    pricetype: 'MARKET' as 'MARKET' | 'LIMIT' | 'SL-M',
    price: '',
  })
  const [qtyOverride, setQtyOverride] = useState<{ ce?: number; pe?: number }>({})
  const lastFireRef = useRef(0)

  /* ── sync feedback ───────────────────────────────────────────────────── */
  const [flash, setFlash] = useState<string | null>(null)
  const flashTimer = useRef<number | undefined>(undefined)
  const showFlash = useCallback((msg: string) => {
    setFlash(msg)
    if (flashTimer.current) window.clearTimeout(flashTimer.current)
    flashTimer.current = window.setTimeout(() => setFlash(null), 8000)
  }, [])
  useEffect(
    () => () => {
      if (flashTimer.current) window.clearTimeout(flashTimer.current)
    },
    []
  )
  const pendingStrike = useRef<{ side: 'CE' | 'PE'; strike: number } | null>(null)

  /* ── sync: chart symbol + advisor alerts ─────────────────────────────── */
  const selRef = useRef({ exchange, underlying, rawSpot: '', rawSpotExch: '' })
  selRef.current.exchange = exchange
  selRef.current.underlying = underlying
  /** The exact instrument the user clicked (index/equity/futures), for the SPOT chart. */
  const [rawSpot, setRawSpot] = useState('')
  /** Its original exchange (NSE / NSE_INDEX / BSE…), which may differ from the F&O one. */
  const [rawSpotExch, setRawSpotExch] = useState('')

  const aimAt = useCallback(
    (exch: TermExchange, und: string, rawSym?: string, rawExch?: string) => {
      const cur = selRef.current
      const setRaw = (sym: string, exch2: string) => {
        if (cur.rawSpot === sym && cur.rawSpotExch === exch2) return
        cur.rawSpot = sym
        cur.rawSpotExch = exch2
        setRawSpot(sym)
        setRawSpotExch(exch2)
      }
      if (cur.exchange === exch && cur.underlying === und) {
        // Same family: still refresh the raw SPOT chart instrument.
        if (rawSym !== undefined) setRaw(rawSym, rawExch ?? cur.rawSpotExch)
        return
      }
      if (rawSym !== undefined) setRaw(rawSym, rawExch ?? '')
      else setRaw('', '')
      setExchange(exch)
      setUnderlying(und)
      setExpiry('')
      setCeStrike('')
      setPeStrike('')
    },
    []
  )

  useEffect(() => {
    return subscribeSync((s) => {
      if (!followChart || !s.symbol) return
      const prefix = s.symbol.split(':')[0] ?? ''
      const info = underlyingFromChart(s.symbol)
      if (!info.underlying) return
      aimAt(exchForChartPrefix(prefix), info.underlying, info.rawSym, prefix.toUpperCase())
      if (info.isOption && info.optionType && info.strike != null) {
        pendingStrike.current = { side: info.optionType, strike: info.strike }
      }
      showFlash(`Following chart: ${s.symbol.split(':')[1] ?? s.symbol}`)
    })
  }, [followChart, showFlash, aimAt])

  const applyTarget = useCallback(
    (t: ScalperTarget) => {
      const und = t.underlying || t.key
      if (!und) return
      aimAt(exchForMarket(t.exchange), und)
      if (t.side && t.strike != null) {
        pendingStrike.current = { side: t.side, strike: t.strike }
      }
      showFlash(`Advisor: ${t.key} BUY ${t.side ?? ''}${t.strike != null ? ` @${t.strike}` : ''} — synced`)
    },
    [showFlash, aimAt]
  )
  useEffect(
    () =>
      subscribeSyncTarget((t) => {
        if (t) applyTarget(t)
      }),
    [applyTarget]
  )

  /* ── underlyings / expiry / chain ────────────────────────────────────── */
  const optionsMode = segment === 'OPTIONS'

  const { data: expiryResp } = useQuery({
    queryKey: ['scalperTerm', 'expiry', exchange, underlying],
    queryFn: () => termApi.getExpiry(underlying, exchange, 'options'),
    enabled: optionsMode && !!underlying,
  })
  const expiries = expiryResp?.data ?? []
  useEffect(() => {
    if (!optionsMode || expiries.length === 0) return
    setExpiry((prev) => (prev && expiries.includes(prev) ? prev : expiries[0]))
  }, [expiries, optionsMode])

  const { data: chainResp, isFetching: chainLoading } = useQuery({
    queryKey: ['scalperTerm', 'strikes', exchange, underlying, expiry, strikeCount],
    queryFn: () => termApi.getStrikes(underlying, exchange, expiry, strikeCount),
    enabled: optionsMode && !!underlying && !!expiry,
  })
  const chain = useMemo(() => chainResp?.chain ?? [], [chainResp])
  const foExchange = chainResp?.fo_exchange ?? exchange
  const underlyingSym = chainResp?.underlying_symbol ?? underlying
  const underlyingExch = chainResp?.underlying_exchange ?? exchange
  const chainLot = useMemo(() => {
    for (const r of chain) {
      const ls = Number(r.ce?.lotsize) || Number(r.pe?.lotsize)
      if (ls) return ls
    }
    return 0
  }, [chain])

  // Default CE/PE to ATM, honouring a pending sync strike when it lands.
  useEffect(() => {
    if (chainResp?.atm_strike == null || chain.length === 0) return
    const strikes = new Set(chain.map((r) => String(r.strike)))
    const atm = String(chainResp.atm_strike)
    const pend = pendingStrike.current
    pendingStrike.current = null
    setCeStrike((prev) => {
      if (pend && pend.side === 'CE' && strikes.has(String(pend.strike))) return String(pend.strike)
      return prev && strikes.has(prev) ? prev : atm
    })
    setPeStrike((prev) => {
      if (pend && pend.side === 'PE' && strikes.has(String(pend.strike))) return String(pend.strike)
      return prev && strikes.has(prev) ? prev : atm
    })
  }, [chainResp, chain])

  /* ── futures mode (single instrument) ────────────────────────────────── */
  const { data: futResp } = useQuery({
    queryKey: ['scalperTerm', 'futures', exchange, underlying],
    queryFn: () => termApi.futures(underlying, exchange),
    enabled: !optionsMode && !!underlying,
  })
  const futContracts = futResp?.data ?? []
  const [futSymbol, setFutSymbol] = useState('')
  useEffect(() => {
    if (optionsMode || futContracts.length === 0) return
    setFutSymbol((prev) => (prev && futContracts.some((c) => c.symbol === prev) ? prev : futContracts[0].symbol))
  }, [futContracts, optionsMode])

  /* ── legs ────────────────────────────────────────────────────────────── */
  const legOf = useCallback(
    (type: 'ce' | 'pe', strikeStr: string): SelectedLeg | null => {
      if (!optionsMode || !strikeStr) return null
      const row = chain.find((r) => String(r.strike) === strikeStr)
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
    },
    [optionsMode, chain, foExchange]
  )
  const ceLeg = legOf('ce', ceStrike)
  const peLeg = legOf('pe', peStrike)
  const futContract = futContracts.find((c) => c.symbol === futSymbol) ?? null

  /* ── positions (for SQUARE) ──────────────────────────────────────────── */
  const { data: posResp } = useQuery({
    queryKey: ['scalperTerm', 'positions'],
    queryFn: () => tradingApi.getPositions(apiKey),
    enabled: !!apiKey,
    refetchOnWindowFocus: true,
  })
  const positions = useMemo(
    () => (posResp?.data ?? []).filter((p) => p.symbol.startsWith(underlying)),
    [posResp, underlying]
  )
  const netQtyOf = useCallback(
    (symbol: string) => {
      const p = positions.find((x) => x.symbol === symbol)
      return p ? Number(p.quantity || 0) : 0
    },
    [positions]
  )

  /* ── live feed: underlying + legs + positions ────────────────────────── */
  const symbols = useMemo(() => {
    const seen = new Set<string>()
    const list: Array<{ symbol: string; exchange: string }> = []
    const add = (symbol: string, exchange: string) => {
      const k = `${exchange}:${symbol}`
      if (!symbol || !exchange || seen.has(k)) return
      seen.add(k)
      list.push({ symbol, exchange })
    }
    if (underlyingSym && underlyingExch) add(underlyingSym, underlyingExch)
    if (rawSpot && (rawSpotExch || underlyingExch)) add(rawSpot, rawSpotExch || underlyingExch)
    if (ceLeg) add(ceLeg.symbol, ceLeg.exchange)
    if (peLeg) add(peLeg.symbol, peLeg.exchange)
    if (!optionsMode && futSymbol) add(futSymbol, exchange)
    for (const p of positions) add(p.symbol, p.exchange)
    return list
  }, [optionsMode, underlyingSym, underlyingExch, rawSpot, rawSpotExch, ceLeg, peLeg, futSymbol, exchange, positions])

  const { data: marketData, isConnected, isAuthenticated, isFallbackMode } = useMarketData({
    symbols,
    mode: 'Quote',
    enabled: symbols.length > 0,
  })

  // After-hours REST fallback, same pattern as the /scalping page.
  const symbolsKey = useMemo(() => symbols.map((s) => `${s.exchange}:${s.symbol}`).join(','), [symbols])
  const [mqMap, setMqMap] = useState<Map<string, QuotesData>>(new Map())
  // biome-ignore lint/correctness/useExhaustiveDependencies: symbolsKey tracks symbols content
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
          for (const r of resp.results) if (r.data) next.set(`${r.exchange}:${r.symbol}`, r.data)
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
      return mergeTick(entry?.data as TickView | undefined, entry?.lastUpdate, mqMap.get(key), Date.now(), 5000)
    },
    [marketData, mqMap]
  )
  const marketDataRef = useRef(marketData)
  marketDataRef.current = marketData

  const undTick = getTick(underlyingSym, underlyingExch)
  /** SPOT column's live tick — the raw clicked instrument when we have one. */
  const rawSpotTick =
    rawSpot && (rawSpotExch || underlyingExch) && rawSpot !== underlyingSym
      ? getTick(rawSpot, rawSpotExch || underlyingExch)
      : undefined
  const spotTick = rawSpotTick ?? undTick
  const ceTick = ceLeg ? getTick(ceLeg.symbol, ceLeg.exchange) : undefined
  const peTick = peLeg ? getTick(peLeg.symbol, peLeg.exchange) : undefined
  const futTick = !optionsMode && futSymbol ? getTick(futSymbol, exchange) : undefined

  /* ── orders (per column) ─────────────────────────────────────────────── */
  const lotOf = (leg: SelectedLeg | null) => {
    if (leg?.lotsize) return leg.lotsize
    if (!optionsMode && futContract?.lotsize) return futContract.lotsize
    return chainLot || 1
  }

  const submitOrder = useCallback(
    async (leg: SelectedLeg | null, action: ScalpingAction, symbolFallback?: string) => {
      if (!armed) {
        showToast.error('One-Click is disarmed — enable it in the top bar to trade', 'orders')
        return
      }
      const symbol = leg?.symbol ?? symbolFallback
      if (!symbol) {
        showToast.error('No instrument selected', 'orders')
        return
      }
      const lotsize = lotOf(leg)
      if (!lotsize) {
        showToast.error('Lot/contract size unavailable', 'orders')
        return
      }
      const side = action === 'BUY' ? 'ce' : 'pe'
      const lots = qtyOverride[side] ?? 1
      const quantity = lots * lotsize
      const now = Date.now()
      if (now - lastFireRef.current < ORDER_COOLDOWN_MS) return
      lastFireRef.current = now

      const exch = leg?.exchange ?? exchange
      const isDerivative = !['NSE', 'BSE'].includes(exch)
      const legLtp = marketDataRef.current.get(`${exch}:${symbol}`)?.data?.ltp
      const req: ScalpingOrderRequest = {
        symbol,
        exchange: exch,
        action,
        quantity,
        product: ordCfg.product,
        ltp: legLtp != null && legLtp > 0 ? legLtp : undefined,
      }
      if (isDerivative) req.lots = lots
      if (ordCfg.pricetype === 'LIMIT' && Number(ordCfg.price) > 0) {
        showToast.error('The scalping API places market orders — use LIMIT from the /scalping page', 'orders')
        return
      }
      try {
        const res = await termApi.placeOrder(req)
        if (res.status === 'success') {
          showToast.success(`${action} ${lots}× ${symbol} placed ✔`)
          queryClient.invalidateQueries({ queryKey: ['scalperTerm', 'positions'] })
        } else {
          showToast.error(res.message ?? 'Order failed', 'orders')
        }
      } catch (e) {
        const err = e as { response?: { data?: { message?: string } }; message?: string }
        showToast.error(err.response?.data?.message || err.message || 'Order failed', 'orders')
      }
    },
    // biome-ignore lint/correctness/useExhaustiveDependencies: submitOrder is stable per config
    [armed, ordCfg, qtyOverride, exchange, queryClient, chainLot, futContract, optionsMode]
  )

  const squareOff = useCallback(
    async (leg: SelectedLeg | null, symbolFallback?: string) => {
      const symbol = leg?.symbol ?? symbolFallback
      if (!symbol) return
      const netQty = netQtyOf(symbol)
      if (netQty === 0) {
        showToast.info?.(`No open position in ${symbol}`)
        return
      }
      if (!confirm(`⚠️ Square off ${Math.abs(netQty)} qty of ${symbol}?`)) return
      const action: ScalpingAction = netQty > 0 ? 'SELL' : 'BUY'
      try {
        const res = await termApi.closeLeg({
          symbol,
          exchange: leg?.exchange ?? exchange,
          action,
          quantity: Math.abs(netQty),
          product: ordCfg.product,
        })
        if (res.status === 'success') {
          showToast.success(`Square off ${symbol} sent ✔`)
          queryClient.invalidateQueries({ queryKey: ['scalperTerm', 'positions'] })
        } else {
          showToast.error(res.message ?? 'Square off failed', 'orders')
        }
      } catch (e) {
        const err = e as { response?: { data?: { message?: string } }; message?: string }
        showToast.error(err.response?.data?.message || err.message || 'Square off failed', 'orders')
      }
    },
    [netQtyOf, ordCfg.product, exchange, queryClient]
  )

  const wsBadge = isFallbackMode
    ? { label: 'Polling', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' }
    : isAuthenticated
      ? { label: 'LIVE', cls: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' }
      : isConnected
        ? { label: '…', cls: 'bg-muted text-muted-foreground' }
        : { label: 'DOWN', cls: 'bg-rose-500/15 text-rose-600 dark:text-rose-400' }

  const dec = priceDecimals(exchange)
  const fmtNum = (v: number | null | undefined, d = dec) => (v == null ? '—' : v.toFixed(d))
  const spotLabel = optionsMode ? rawSpot || underlyingSym || underlying : futSymbol
  /** The SPOT chart/depth instrument: raw clicked symbol, else chain resolution. */
  const spotSym = optionsMode ? rawSpot || underlyingSym || '' : futSymbol
  /** SPOT's exchange: the raw instrument's own (NSE / NSE_INDEX / BSE…),
   * else the chain's underlying exchange — never the F&O options exchange,
   * which has no equities/indices and made every raw load fail silently. */
  const spotExch = optionsMode
    ? rawSpot
      ? rawSpotExch || underlyingExch || 'NSE'
      : underlyingExch || 'NSE'
    : exchange

  return (
    <div
      data-trading-scalper-terminal
      className="absolute left-2 top-12 z-30 flex max-h-[calc(100%-3.5rem)] min-h-[560px] w-[min(1180px,calc(100%-1rem))] flex-col overflow-hidden rounded-lg border bg-background/95 shadow-xl backdrop-blur-sm"
    >
      {/* Header: identity + sync */}
      <div className="flex items-center gap-1.5 border-b bg-muted/30 px-2 py-1">
        <span className="text-[11px] font-bold uppercase tracking-wide text-foreground">Scalper</span>
        <Select
          value={exchange}
          onValueChange={(v) => {
            selRef.current.rawSpot = ''
            selRef.current.rawSpotExch = ''
            setRawSpot('')
            setRawSpotExch('')
            setExchange(v as TermExchange)
            setUnderlying(DEFAULT_UNDERLYING[v as TermExchange])
            setExpiry('')
            setCeStrike('')
            setPeStrike('')
          }}
        >
          <SelectTrigger className="h-5 w-[70px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Exchange">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="max-h-40">
            {EXCHANGES.map((x) => (
              <SelectItem key={x} value={x} className="text-[11px]">{x}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={segment} onValueChange={(v) => setSegment(v as Segment)}>
          <SelectTrigger className="h-5 w-[78px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Segment">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="OPTIONS" className="text-[11px]">Options</SelectItem>
            <SelectItem value="FUTURES" className="text-[11px]">Futures</SelectItem>
          </SelectContent>
        </Select>
        <UnderlyingPicker
          exchange={exchange}
          value={underlying}
          onPick={(u) => {
            selRef.current.rawSpot = ''
            selRef.current.rawSpotExch = ''
            setRawSpot('')
            setRawSpotExch('')
            setUnderlying(u)
            setExpiry('')
            setCeStrike('')
            setPeStrike('')
          }}
        />
        {optionsMode ? (
          <Select value={expiry || undefined} onValueChange={setExpiry} disabled={expiries.length === 0}>
            <SelectTrigger className="h-5 w-[88px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Expiry">
              <SelectValue placeholder="expiry" />
            </SelectTrigger>
            <SelectContent className="max-h-48">
              {expiries.map((e) => (
                <SelectItem key={e} value={e} className="text-[11px]">{e}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <span className="truncate font-mono text-[10px] text-muted-foreground">{futSymbol || '…'}</span>
        )}
        {optionsMode && (
          <button
            type="button"
            onClick={() => {
              const atm = chainResp?.atm_strike
              if (atm != null) {
                setCeStrike(String(atm))
                setPeStrike(String(atm))
              }
            }}
            className="rounded border border-border px-1.5 py-px text-[9px] font-bold text-muted-foreground hover:text-foreground"
            title="Reset CE & PE to ATM"
          >
            ⚡ ATM
          </button>
        )}
        <button
          type="button"
          onClick={() => setStrikeCount((n) => (n >= 16 ? 4 : n + 4))}
          className="rounded border border-border px-1 py-px text-[9px] font-semibold text-muted-foreground hover:text-foreground"
          title="Strikes per side"
        >
          ×{strikeCount}
        </button>

        {spotTick?.ltp != null && spotLabel && (
          <span className="font-mono text-[10px] font-semibold tabular-nums text-foreground">
            {underlying}{' '}
            <span className={(spotTick.change_percent ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>
              {fmtNum(spotTick.ltp)} {(spotTick.change_percent ?? 0) >= 0 ? '+' : ''}
              {(spotTick.change_percent ?? 0).toFixed(2)}%
            </span>
          </span>
        )}

        <div className="ml-auto flex items-center gap-1">
          <span className={cn('rounded px-1 py-px text-[9px] font-bold', wsBadge.cls)}>{wsBadge.label}</span>
          <button
            type="button"
            onClick={() => setFollowChart((v) => !v)}
            className={cn(
              'rounded border px-1 py-px text-[9px] font-semibold',
              followChart ? 'border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400' : 'border-border text-muted-foreground'
            )}
            title="Follow the focused chart pane's symbol"
          >
            {followChart ? 'SYNC ●' : 'SYNC ○'}
          </button>
          <button type="button" onClick={onClose} className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground" title="Close scalper">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* One FIXED-height status slot — flash, loading and chain notices share
          it so nothing below ever reflows (insert/remove rows stole clicks). */}
      <div className="h-4 shrink-0 overflow-hidden border-b border-border/40 px-2 text-[10px] leading-4">
        {flash ? (
          <span className="font-medium text-sky-600 dark:text-sky-400">⚡ {flash}</span>
        ) : chainLoading && optionsMode ? (
          <span className="text-muted-foreground">Loading chain…</span>
        ) : !chainLoading && optionsMode && !expiry && expiries.length === 0 ? (
          <span className="text-amber-600 dark:text-amber-400">
            No F&amp;O options for this symbol — SPOT charts it live; pick an index/futures root for CE·PE trading
          </span>
        ) : null}
      </div>

      {/* ── three columns: CE · SPOT · PE ─────────────────────────────── */}
      <div className="grid min-h-0 flex-1 grid-cols-3 gap-1.5 overflow-y-auto p-1.5">
        <Column
          side="ce"
          label="CE"
          badgeCls="bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
          accent="#089981"
          symbol={ceLeg?.symbol ?? ''}
          exchange={ceLeg?.exchange ?? foExchange}
          lotsize={lotOf(ceLeg)}
          tick={ceTick}
          dec={dec}
          chartH={chartH}
          onDragStart={onDragStart}
          positionsQty={ceLeg ? netQtyOf(ceLeg.symbol) : 0}
          apiKey={apiKey}
          wsUrl={wsUrl}
          columnId="ce"
          depthEnabled={!!ceLeg}
          ordCfg={ordCfg}
          setOrdCfg={setOrdCfg}
          lots={qtyOverride.ce ?? 1}
          setLots={(n) => setQtyOverride((p) => ({ ...p, ce: n }))}
          onBuy={() => submitOrder(ceLeg, 'BUY')}
          onSell={() => submitOrder(ceLeg, 'SELL')}
          onSquare={() => squareOff(ceLeg)}
          headerExtra={
            <Select
              value={ceStrike || undefined}
              onValueChange={setCeStrike}
              disabled={chain.length === 0}
            >
              <SelectTrigger className="h-5 w-[86px] border-border/60 bg-transparent px-1 text-[10px] shadow-none focus:ring-0" aria-label="CE strike">
                <SelectValue placeholder="strike" />
              </SelectTrigger>
              <SelectContent className="max-h-48">
                {chain.map((r) => (
                  <SelectItem key={`ce-${r.strike}`} value={String(r.strike)} className="text-[11px]">
                    {Number(r.strike).toLocaleString('en-IN')}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          }
        />

        <Column
          side="spot"
          label="SPOT"
          badgeCls="bg-sky-500/15 text-sky-600 dark:text-sky-400"
          accent="#2962ff"
          symbol={spotSym}
          exchange={spotExch}
          lotsize={optionsMode ? 0 : lotOf(null)}
          tick={optionsMode ? spotTick : futTick}
          dec={priceDecimals(spotExch)}
          chartH={chartH}
          onDragStart={onDragStart}
          positionsQty={optionsMode ? 0 : netQtyOf(futSymbol)}
          apiKey={apiKey}
          wsUrl={wsUrl}
          columnId="spot"
          depthEnabled={!!spotSym}
          ordCfg={ordCfg}
          setOrdCfg={setOrdCfg}
          lots={qtyOverride.pe ?? 1}
          setLots={(n) => setQtyOverride((p) => ({ ...p, pe: n }))}
          onBuy={() =>
            optionsMode
              ? showToast.info?.('Spot is reference-only — trade the CE/PE columns')
              : submitOrder(null, 'BUY', futSymbol)
          }
          onSell={() =>
            optionsMode
              ? showToast.info?.('Spot is reference-only — trade the CE/PE columns')
              : submitOrder(null, 'SELL', futSymbol)
          }
          onSquare={() => (optionsMode ? showToast.info?.('Spot is reference-only') : squareOff(null, futSymbol))}
          headerExtra={
            !optionsMode ? (
              <span className="rounded border border-border/60 px-1 py-px text-[9px] text-muted-foreground">
                lot {futContract?.lotsize ?? '—'}
              </span>
            ) : (
              <span className="rounded border border-border/60 px-1 py-px text-[9px] text-muted-foreground">
                {underlyingExch === 'NSE' && underlyingSym?.includes('INDEX') ? 'index' : 'fut'}
              </span>
            )
          }
        />

        <Column
          side="pe"
          label="PE"
          badgeCls="bg-rose-500/15 text-rose-600 dark:text-rose-400"
          accent="#f23645"
          symbol={peLeg?.symbol ?? ''}
          exchange={peLeg?.exchange ?? foExchange}
          lotsize={lotOf(peLeg)}
          tick={peTick}
          dec={dec}
          chartH={chartH}
          onDragStart={onDragStart}
          positionsQty={peLeg ? netQtyOf(peLeg.symbol) : 0}
          apiKey={apiKey}
          wsUrl={wsUrl}
          columnId="pe"
          depthEnabled={!!peLeg}
          ordCfg={ordCfg}
          setOrdCfg={setOrdCfg}
          lots={qtyOverride.pe ?? 1}
          setLots={(n) => setQtyOverride((p) => ({ ...p, pe: n }))}
          onBuy={() => submitOrder(peLeg, 'BUY')}
          onSell={() => submitOrder(peLeg, 'SELL')}
          onSquare={() => squareOff(peLeg)}
          headerExtra={
            <Select
              value={peStrike || undefined}
              onValueChange={setPeStrike}
              disabled={chain.length === 0}
            >
              <SelectTrigger className="h-5 w-[86px] border-border/60 bg-transparent px-1 text-[10px] shadow-none focus:ring-0" aria-label="PE strike">
                <SelectValue placeholder="strike" />
              </SelectTrigger>
              <SelectContent className="max-h-48">
                {chain.map((r) => (
                  <SelectItem key={`pe-${r.strike}`} value={String(r.strike)} className="text-[11px]">
                    {Number(r.strike).toLocaleString('en-IN')}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          }
        />
      </div>

      {/* Footer strip */}
      <div className="flex items-center justify-between border-t bg-muted/20 px-2 py-1 text-[9px] text-muted-foreground">
        <span>
          {positions.length > 0 &&
            `${positions.length} position${positions.length > 1 ? 's' : ''} in ${underlying}`}
          {positions.length === 0 && 'No positions in this underlying'}
        </span>
        <span>Charts stream live · depth polls REST every 3s · orders use the scalping lot-cap</span>
      </div>
    </div>
  )
}

/* ═══════════════ one column: chart + depth + orders ═══════════════ */

type OrdCfg = {
  product: ScalpingProduct
  pricetype: 'MARKET' | 'LIMIT' | 'SL-M'
  price: string
}

function Column({
  side,
  label,
  badgeCls,
  accent,
  symbol,
  exchange,
  lotsize,
  tick,
  dec,
  chartH,
  onDragStart,
  positionsQty,
  apiKey,
  wsUrl,
  columnId,
  depthEnabled,
  ordCfg,
  setOrdCfg,
  lots,
  setLots,
  onBuy,
  onSell,
  onSquare,
  headerExtra,
}: {
  side: 'ce' | 'spot' | 'pe'
  label: string
  badgeCls: string
  accent: string
  symbol: string
  exchange: string
  lotsize: number
  tick: TickView | undefined
  dec: number
  chartH: number
  onDragStart: (e: React.MouseEvent | React.TouchEvent) => void
  positionsQty: number
  apiKey: string
  wsUrl: string
  columnId: string
  depthEnabled: boolean
  ordCfg: OrdCfg
  setOrdCfg: React.Dispatch<React.SetStateAction<OrdCfg>>
  lots: number
  setLots: (n: number) => void
  onBuy: () => void
  onSell: () => void
  onSquare: () => void
  headerExtra?: React.ReactNode
}) {
  const isTradable = side !== 'spot' || !!symbol
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-md border border-border/70 bg-background/60 p-1">
      {/* header: badge + ltp + strike */}
      <div className="flex items-center gap-1">
        <span className={cn('rounded px-1.5 py-px text-[10px] font-bold', badgeCls)}>{label}</span>
        <span
          className={cn(
            'rounded border border-border/60 bg-muted/40 px-1 py-px font-mono text-[10px] font-bold tabular-nums',
            side === 'ce' ? 'text-emerald-600 dark:text-emerald-400' : side === 'pe' ? 'text-rose-600 dark:text-rose-400' : 'text-sky-600 dark:text-sky-400'
          )}
          style={{ color: accent }}
        >
          {tick?.ltp != null ? fmtCol(tick.ltp, dec) : '—'}
          {tick?.change_percent != null && (
            <span className={cn('ml-1 text-[8px] font-semibold', tick.change_percent >= 0 ? 'text-emerald-600/80' : 'text-rose-600/80')}>
              {tick.change_percent >= 0 ? '+' : ''}
              {tick.change_percent.toFixed(1)}%
            </span>
          )}
        </span>
        {positionsQty !== 0 && (
          <span className="rounded bg-primary/10 px-1 py-px text-[9px] font-bold text-primary" title="Open net qty">
            {positionsQty > 0 ? '+' : ''}
            {positionsQty}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">{headerExtra}</div>
      </div>

      {/* chart (the same OpenAlgo engine as the main grid) + resizer */}
      <div className="relative shrink-0 overflow-hidden rounded border border-border/60 bg-card" style={{ height: chartH }}>
        {symbol && exchange ? (
          <ScalperChart apiKey={apiKey} wsUrl={wsUrl} symbol={symbol} exchange={exchange} columnId={columnId} />
        ) : (
          <div className="flex h-full items-center justify-center text-[10px] text-muted-foreground">Pick a strike…</div>
        )}
      </div>
      <div
        onMouseDown={onDragStart}
        onTouchStart={onDragStart}
        className="flex h-1.5 shrink-0 cursor-ns-resize items-center justify-center rounded-sm border-y border-border/50 bg-muted/30"
        title="Drag to resize all charts"
      >
        <div className="h-0.5 w-7 rounded bg-muted-foreground/40" />
      </div>

      {/* 5-level depth with qty bars */}
      <DepthTable apiKey={apiKey} symbol={symbol} exchange={exchange} enabled={depthEnabled} />

      {/* order controls */}
      <div className="mt-auto space-y-1 rounded border border-border/60 bg-muted/20 p-1">
        <div className="flex items-center gap-1">
          <div className="flex items-center gap-0.5" title={lotsize ? `lot ${lotsize}` : 'lot size pending'}>
            <button type="button" onClick={() => setLots(Math.max(1, lots - 1))} className="rounded border border-border px-1.5 text-[10px] hover:bg-accent">−</button>
            <span className="w-5 text-center font-mono text-[10px] tabular-nums">{lots}</span>
            <button type="button" onClick={() => setLots(Math.min(20, lots + 1))} className="rounded border border-border px-1.5 text-[10px] hover:bg-accent">+</button>
          </div>
          <Select value={ordCfg.pricetype} onValueChange={(v) => setOrdCfg((p) => ({ ...p, pricetype: v as OrdCfg['pricetype'] }))}>
            <SelectTrigger className="h-5 w-[64px] border-border/60 bg-transparent px-1 text-[9px] shadow-none focus:ring-0" aria-label="Order type">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="MARKET" className="text-[10px]">MKT</SelectItem>
              <SelectItem value="LIMIT" className="text-[10px]" disabled title="Market only on this terminal">LMT</SelectItem>
              <SelectItem value="SL-M" className="text-[10px]" disabled title="Market only on this terminal">SL-M</SelectItem>
            </SelectContent>
          </Select>
          <Select value={ordCfg.product} onValueChange={(v) => setOrdCfg((p) => ({ ...p, product: v as ScalpingProduct }))}>
            <SelectTrigger className="h-5 w-[56px] border-border/60 bg-transparent px-1 text-[9px] shadow-none focus:ring-0" aria-label="Product">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="MIS" className="text-[10px]">MIS</SelectItem>
              <SelectItem value="NRML" className="text-[10px]">NRML</SelectItem>
            </SelectContent>
          </Select>
          {lotsize > 0 && <span className="ml-auto text-[8px] text-muted-foreground">×{lotsize} = {lots * lotsize}</span>}
        </div>
        <div className="grid grid-cols-2 gap-1">
          <Button
            disabled={!isTradable || !symbol}
            onClick={onBuy}
            className="h-6 bg-emerald-600 px-1 text-[10px] font-bold hover:bg-emerald-700"
          >
            BUY
          </Button>
          <Button
            disabled={!isTradable || !symbol}
            onClick={onSell}
            className="h-6 bg-rose-600 px-1 text-[10px] font-bold hover:bg-rose-700"
          >
            SELL
          </Button>
        </div>
        <Button
          variant="outline"
          disabled={!symbol || positionsQty === 0}
          onClick={onSquare}
          className="h-5 w-full border-amber-500/40 bg-amber-500/10 px-1 text-[9px] font-bold text-amber-600 hover:bg-amber-500/20 dark:text-amber-400"
          title="Flatten this column's open position"
        >
          {positionsQty !== 0 ? `SQUARE ${Math.abs(positionsQty)} qty` : 'SQUARE'}
        </Button>
      </div>
    </div>
  )
}

function fmtCol(v: number, d: number) {
  return v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })
}

/* ═══════════════ 5-level depth table (REST poll, qty bars) ═══════════════ */

function DepthTable({
  apiKey,
  symbol,
  exchange,
  enabled,
}: {
  apiKey: string
  symbol: string
  exchange: string
  enabled: boolean
}) {
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
        // transient — keep the last book
      }
    }
    fetchDepth()
    timer = window.setInterval(fetchDepth, 3000)
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
    return <div className="flex min-h-[70px] flex-1 items-center justify-center rounded border border-border/40 text-[9px] text-muted-foreground">—</div>
  }

  const bids = depth?.bids ?? []
  const asks = depth?.asks ?? []
  if (!bids.length && !asks.length) {
    return (
      <div className="flex min-h-[70px] flex-1 items-center justify-center rounded border border-border/40 px-2 text-center text-[9px] text-muted-foreground">
        {dead ? 'Depth unavailable' : exchange === 'NSE' && symbol.includes('INDEX') ? 'Spot index — no order book' : 'Waiting for depth…'}
      </div>
    )
  }

  const maxQ = Math.max(
    ...bids.map((b) => b.quantity || 0),
    ...asks.map((a) => a.quantity || 0),
    1
  )

  return (
    <div className="min-h-[70px] flex-1 rounded border border-border/40 bg-muted/10 p-0.5">
      <table className="w-full table-fixed border-collapse text-[10px]">
        <thead>
          <tr className="text-[8px] uppercase tracking-wide text-muted-foreground">
            <th className="w-[26%] py-px text-left font-semibold">B-Qty</th>
            <th className="w-[24%] py-px text-right font-semibold text-emerald-600 dark:text-emerald-400">Bid</th>
            <th className="w-[24%] border-l border-border/40 py-px pl-1 text-left font-semibold text-rose-600 dark:text-rose-400">Ask</th>
            <th className="w-[26%] py-px text-right font-semibold">A-Qty</th>
          </tr>
        </thead>
        <tbody className="font-mono tabular-nums">
          {[0, 1, 2, 3, 4].map((i) => {
            const b = bids[i]
            const a = asks[i]
            return (
              <tr key={i} className="border-b border-border/20 last:border-0">
                <td className="relative px-1 py-px text-left font-bold text-emerald-600 dark:text-emerald-400">
                  {b && <span className="absolute inset-y-0 left-0 bg-emerald-500/15" style={{ width: `${((b.quantity || 0) / maxQ) * 100}%` }} />}
                  <span className="relative">{b ? b.quantity.toLocaleString('en-IN') : '—'}</span>
                </td>
                <td className="px-1 py-px text-right font-bold text-emerald-600 dark:text-emerald-400">{b ? b.price : '—'}</td>
                <td className="border-l border-border/40 px-1 py-px pl-1 text-left font-bold text-rose-600 dark:text-rose-400">{a ? a.price : '—'}</td>
                <td className="relative px-1 py-px text-right font-bold text-rose-600 dark:text-rose-400">
                  {a && <span className="absolute inset-y-0 right-0 bg-rose-500/15" style={{ width: `${((a.quantity || 0) / maxQ) * 100}%` }} />}
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

/** Underlying combobox over /scalping/api/all_underlyings — indices first. */
function UnderlyingPicker({
  exchange,
  value,
  onPick,
}: {
  exchange: TermExchange
  value: string
  onPick(u: string): void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const { data } = useQuery({
    queryKey: ['scalperTerm', 'allund', exchange],
    queryFn: () => termApi.getAllUnderlyings(exchange, 'options'),
    staleTime: 5 * 60 * 1000,
  })
  const list = data?.data ?? []
  const matches = useMemo(() => {
    const q = query.trim().toUpperCase()
    const filtered = q ? list.filter((u) => u.toUpperCase().includes(q)) : list
    return filtered.slice(0, 120)
  }, [list, query])

  return (
    <div className="relative">
      <input
        value={open ? query : value}
        placeholder="underlying"
        aria-label="Underlying"
        onFocus={() => {
          setQuery('')
          setOpen(true)
        }}
        onChange={(e) => setQuery(e.target.value)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && matches.length > 0) {
            onPick(matches[0])
            setOpen(false)
            ;(e.target as HTMLInputElement).blur()
          }
        }}
        className="h-5 w-[104px] rounded border border-border/60 bg-transparent px-1 text-[10px] font-semibold text-foreground outline-none focus:border-primary"
      />
      {open && matches.length > 0 && (
        <div className="absolute left-0 top-6 z-40 max-h-60 w-44 overflow-auto rounded-md border bg-popover shadow-md">
          {matches.map((u) => (
            <button
              type="button"
              key={u}
              className={cn('block w-full px-2 py-0.5 text-left font-mono text-[10px] hover:bg-muted', u === value && 'bg-muted')}
              onMouseDown={(ev) => ev.preventDefault()}
              onClick={() => {
                onPick(u)
                setOpen(false)
              }}
            >
              {u}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default ScalperTerminal
