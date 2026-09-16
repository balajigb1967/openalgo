import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { scalpingApi } from '@/api/scalping'
import { type QuotesData, tradingApi } from '@/api/trading'
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
import { useMarketData } from '@/hooks/useMarketData'
import { StrikeDepthCell } from './StrikeDepthCell'
import type {
  OptionChainRow,
  ScalpingAction,
  ScalpingProduct,
  SelectedLeg,
  Segment,
} from '@/types/scalping'
import { showToast } from '@/utils/toast'

/**
 * Compact scalper terminal, embedded over the Trading page's chart area.
 *
 * The /scalping page's derivative workflow — underlying/expiry/chain, dual-leg
 * CE/PE with live ticks and one-click market orders — reduced to what fits
 * beside a chart. It follows two sync channels (lib/scalperSync): the focused
 * chart pane's symbol, and the sidebar Scalper Advisor's alerts, so a BUY
 * alert or a chart focus change re-aims the whole ladder without touching it.
 *
 * Market depth per strike comes from StrikeDepthCell's REST poll — the
 * proven-live path on this deployment (the broker websocket never pushes
 * depth for MCX here).
 */

const MAX_LOTS = 20
const ORDER_COOLDOWN_MS = 120

type TermExchange = 'NFO' | 'BFO' | 'MCX' | 'CDS'
const EXCHANGES: TermExchange[] = ['NFO', 'BFO', 'MCX', 'CDS']
const DEFAULT_UNDERLYING: Record<TermExchange, string> = {
  NFO: 'NIFTY',
  BFO: 'SENSEX',
  MCX: 'CRUDEOIL',
  CDS: 'USDINR',
}

/** Market (advisor) → leg exchange. */
function exchForMarket(m: string | null | undefined): TermExchange {
  return ({ NSE: 'NFO', BSE: 'BFO', MCX: 'MCX', CDS: 'CDS' } as Record<string, TermExchange>)[
    (m || '').toUpperCase()
  ] ?? 'NFO'
}

/** Chart symbol prefix → leg exchange (stocks trade options on the F&O arm). */
function exchForChartPrefix(p: string): TermExchange {
  return ({ NSE: 'NFO', NFO: 'NFO', BSE: 'BFO', BFO: 'BFO', MCX: 'MCX', CDS: 'CDS' } as Record<
    string,
    TermExchange
  >)[p.toUpperCase()] ?? 'NFO'
}

/**
 * Underlying family from a chart symbol: peels the option strike/side, the
 * weekly/monthly tokens (…26OCT…, …25SEP…) and trailing year digits, so
 * NIFTY26OCT24500CE and CRUDEOIL21SEP26FUT both resolve to a chain-able
 * underlying (NIFTY / CRUDEOIL).
 */
function underlyingFromChart(symbol: string): { underlying: string; isOption: boolean; isFut: boolean; optionType: 'CE' | 'PE' | null; strike: number | null } {
  const sym = symbol.split(':')[1] ?? symbol
  const upper = sym.toUpperCase()
  const m = upper.match(/^(.+?)(\d+(?:\.\d+)?)(CE|PE)$/)
  if (m) {
    return {
      underlying: cleanRoot(m[1]),
      isOption: true,
      isFut: false,
      optionType: m[3] as 'CE' | 'PE',
      strike: Number(m[2]),
    }
  }
  return {
    underlying: cleanRoot(upper.replace(/FUT$/, '')),
    isOption: false,
    isFut: upper.endsWith('FUT') || upper.includes('FUT'),
    optionType: null,
    strike: null,
  }
}

function cleanRoot(s: string): string {
  // Month tokens with any digit run after them (26OCT24500, 21SEP26…), then
  // trailing year digits (NIFTY26 → NIFTY).
  return s
    .replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d*/g, '')
    .replace(/\d+$/, '')
}

interface Props {
  apiKey: string
  armed: boolean
  onClose: () => void
}

export function ScalperTerminal({ apiKey, armed, onClose }: Props) {
  const queryClient = useQueryClient()

  /* ── selection state ─────────────────────────────────────────────────── */
  const [exchange, setExchange] = useState<TermExchange>('NFO')
  const [segment, setSegment] = useState<Segment>('OPTIONS')
  const [underlying, setUnderlying] = useState('NIFTY')
  const [expiry, setExpiry] = useState('')
  const [ceStrike, setCeStrike] = useState('')
  const [peStrike, setPeStrike] = useState('')
  const [strikeCount, setStrikeCount] = useState(6)
  const [followChart, setFollowChart] = useState(true)
  const [futInstrument, setFutInstrument] = useState<SelectedLeg | null>(null)

  /* ── order state ─────────────────────────────────────────────────────── */
  const [lots, setLots] = useState(1)
  const [product, setProduct] = useState<ScalpingProduct>('NRML')
  const [depthOn, setDepthOn] = useState(true)
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

  /** Pending side-strike to apply once the new chain arrives (from sync). */
  const pendingStrike = useRef<{ side: 'CE' | 'PE'; strike: number } | null>(null)

  /** Current selection, readable inside sync callbacks without stale closures. */
  const selRef = useRef({ exchange, underlying })
  selRef.current.exchange = exchange
  selRef.current.underlying = underlying

  /** Re-aim the ladder at (exchange, underlying), resetting dependent picks. */
  const aimAt = useCallback((exch: TermExchange, und: string) => {
    const cur = selRef.current
    if (cur.exchange === exch && cur.underlying === und) return
    setExchange(exch)
    setUnderlying(und)
    setExpiry('')
    setCeStrike('')
    setPeStrike('')
    setFutInstrument(null)
  }, [])

  /* ── chart sync: follow the focused pane's symbol ────────────────────── */
  useEffect(() => {
    return subscribeSync((s) => {
      if (!followChart || !s.symbol) return
      const prefix = s.symbol.split(':')[0] ?? ''
      const info = underlyingFromChart(s.symbol)
      if (!info.underlying) return
      const exch = exchForChartPrefix(prefix)
      aimAt(exch, info.underlying)
      if (info.isOption && info.optionType && info.strike != null) {
        pendingStrike.current = { side: info.optionType, strike: info.strike }
      }
      showFlash(`Following chart: ${s.symbol.split(':')[1] ?? s.symbol}`)
    })
  }, [followChart, showFlash, aimAt])

  /* ── advisor sync: alerts published by the sidebar panel ─────────────── */
  const applyTarget = useCallback(
    (t: ScalperTarget) => {
      const und = t.underlying || t.key
      if (!und) return
      aimAt(exchForMarket(t.exchange), und)
      if (t.side && t.strike != null) {
        pendingStrike.current = { side: t.side, strike: t.strike }
      }
      showFlash(`Advisor: ${t.key} BUY ${t.side ?? ''}${t.strike != null ? ` @${t.strike}` : ''} — chain synced`)
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
    queryFn: () => scalpingApi.getExpiry(underlying, exchange, 'options'),
    enabled: optionsMode && !!underlying,
  })
  const expiries = expiryResp?.data ?? []

  // Default to the nearest expiry whenever the list changes and the current
  // pick is missing from it (first load, underlying change, sync re-aim).
  useEffect(() => {
    if (!optionsMode || expiries.length === 0) return
    setExpiry((prev) => (prev && expiries.includes(prev) ? prev : expiries[0]))
  }, [expiries, optionsMode])

  const { data: chainResp, isFetching: chainLoading } = useQuery({
    queryKey: ['scalperTerm', 'strikes', exchange, underlying, expiry, strikeCount],
    queryFn: () => scalpingApi.getStrikes(underlying, exchange, expiry, strikeCount),
    enabled: optionsMode && !!underlying && !!expiry,
  })
  const chain = useMemo(() => chainResp?.chain ?? [], [chainResp])
  const foExchange = chainResp?.fo_exchange ?? exchange
  const underlyingSym = chainResp?.underlying_symbol ?? underlying
  const underlyingExch = chainResp?.underlying_exchange ?? exchange

  // Futures mode: nearest contract (compact terminal has no manual futures
  // picker — the ladder IS the selection).
  const { data: futResp } = useQuery({
    queryKey: ['scalperTerm', 'futures', exchange, underlying],
    queryFn: () => scalpingApi.futures(underlying, exchange),
    enabled: !optionsMode && !!underlying,
  })
  const futContracts = futResp?.data ?? []
  useEffect(() => {
    if (optionsMode || futContracts.length === 0) return
    setFutInstrument((prev) => {
      const stillValid = prev && futContracts.some((c) => c.symbol === prev.symbol)
      if (stillValid) return prev
      const c = futContracts[0]
      return {
        symbol: c.symbol,
        exchange,
        optionType: 'CE',
        strike: 0,
        lotsize: c.lotsize || 1,
        tickSize: c.tick_size ?? 0,
      }
    })
  }, [futContracts, optionsMode, exchange])

  // Default CE/PE strikes to ATM, then honour a pending sync strike.
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

  /* ── legs ────────────────────────────────────────────────────────────── */
  const buildLeg = useCallback(
    (row: OptionChainRow | undefined, type: 'ce' | 'pe'): SelectedLeg | null => {
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
    [foExchange]
  )
  const ceLeg = useMemo(() => (optionsMode ? buildLeg(chain.find((r) => String(r.strike) === ceStrike), 'ce') : null), [optionsMode, buildLeg, chain, ceStrike])
  const peLeg = useMemo(() => (optionsMode ? buildLeg(chain.find((r) => String(r.strike) === peStrike), 'pe') : null), [optionsMode, buildLeg, chain, peStrike])
  const activeLeg: SelectedLeg | null = optionsMode ? null : futInstrument

  /* ── positions (scoped to this underlying) for the MTM strip ─────────── */
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

  /* ── live feed: underlying + both legs + this underlying's positions ─── */
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
    if (activeLeg) add(activeLeg.symbol, activeLeg.exchange)
    for (const p of positions) add(p.symbol, p.exchange)
    return list
  }, [optionsMode, underlyingSym, underlyingExch, ceLeg, peLeg, activeLeg, positions])

  const { data: marketData, isConnected, isAuthenticated, isFallbackMode } = useMarketData({
    symbols,
    mode: 'Quote',
    enabled: symbols.length > 0,
  })

  // After-hours REST fallback (same pattern as the /scalping page): 30s
  // MultiQuotes poll only when ticks go stale, so prices stay honest.
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
  const ceTick = ceLeg ? getTick(ceLeg.symbol, ceLeg.exchange) : undefined
  const peTick = peLeg ? getTick(peLeg.symbol, peLeg.exchange) : undefined
  const futTick = activeLeg ? getTick(activeLeg.symbol, activeLeg.exchange) : undefined

  const openMtm = useMemo(() => {
    let mtm = 0
    let qty = 0
    for (const p of positions) {
      const ltp = getTick(p.symbol, p.exchange)?.ltp
      if (ltp == null) continue
      mtm += (ltp - Number(p.average_price || 0)) * Number(p.quantity || 0)
      qty += Number(p.quantity || 0)
    }
    return { mtm, qty }
  }, [positions, getTick])

  /* ── orders (same conventions as the /scalping page) ─────────────────── */
  const submitOrder = useCallback(
    async (leg: SelectedLeg | null, action: ScalpingAction) => {
      if (!armed) {
        showToast.error('One-Click is disarmed — enable it in the top bar to trade', 'orders')
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
      const legLtp = marketDataRef.current.get(`${leg.exchange}:${leg.symbol}`)?.data?.ltp
      try {
        const res = await scalpingApi.placeOrder({
          symbol: leg.symbol,
          exchange: leg.exchange,
          action,
          quantity,
          product,
          lots,
          ltp: legLtp != null && legLtp > 0 ? legLtp : undefined,
        })
        if (res.status === 'success') {
          queryClient.invalidateQueries({ queryKey: ['scalperTerm', 'positions'] })
        } else {
          showToast.error(res.message ?? 'Order failed', 'orders')
        }
      } catch (e) {
        const err = e as { response?: { data?: { message?: string } }; message?: string }
        showToast.error(err.response?.data?.message || err.message || 'Order failed', 'orders')
      }
    },
    [armed, lots, product, queryClient]
  )

  const doCloseAll = useCallback(async () => {
    try {
      await scalpingApi.closeAll()
      queryClient.invalidateQueries({ queryKey: ['scalperTerm', 'positions'] })
    } catch {
      /* global socket handler toasts */
    }
  }, [queryClient])

  const doCancelAll = useCallback(async () => {
    try {
      await scalpingApi.cancelAll()
    } catch {
      /* global socket handler toasts */
    }
  }, [])

  const wsBadge = isFallbackMode
    ? { label: 'Polling', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' }
    : isAuthenticated
      ? { label: 'LIVE', cls: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' }
      : isConnected
        ? { label: '…', cls: 'bg-muted text-muted-foreground' }
        : { label: 'DOWN', cls: 'bg-rose-500/15 text-rose-600 dark:text-rose-400' }

  const dec = priceDecimals(exchange)
  const fmtNum = (v: number | null | undefined, d = dec) => (v == null ? '—' : v.toFixed(d))

  return (
    <div
      data-trading-scalper-terminal
      className="absolute left-2 top-12 z-30 flex max-h-[calc(100%-3.5rem)] w-[min(780px,calc(100%-1rem))] flex-col overflow-hidden rounded-lg border bg-background/95 shadow-xl backdrop-blur-sm"
    >
      {/* Header: identity + sync chips */}
      <div className="flex items-center gap-1.5 border-b bg-muted/30 px-2 py-1">
        <span className="text-[11px] font-bold uppercase tracking-wide text-foreground">Scalper</span>
        <span className="rounded bg-primary/10 px-1 py-px text-[9px] font-semibold text-primary">
          {exchange}
        </span>
        <Select value={exchange} onValueChange={(v) => {
          setExchange(v as TermExchange)
          setUnderlying(DEFAULT_UNDERLYING[v as TermExchange])
          setExpiry('')
          setCeStrike('')
          setPeStrike('')
          setFutInstrument(null)
        }}>
          <SelectTrigger className="h-5 w-[74px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Exchange">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="max-h-40">
            {EXCHANGES.map((x) => (
              <SelectItem key={x} value={x} className="text-[11px]">
                {x}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={segment} onValueChange={(v) => setSegment(v as Segment)}>
          <SelectTrigger className="h-5 w-[80px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Segment">
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
            setUnderlying(u)
            setExpiry('')
            setCeStrike('')
            setPeStrike('')
            setFutInstrument(null)
          }}
        />
        {optionsMode ? (
          <Select value={expiry || undefined} onValueChange={setExpiry} disabled={expiries.length === 0}>
            <SelectTrigger className="h-5 w-[92px] border-0 bg-transparent px-1.5 text-[10px] shadow-none focus:ring-0" aria-label="Expiry">
              <SelectValue placeholder="expiry" />
            </SelectTrigger>
            <SelectContent className="max-h-48">
              {expiries.map((e) => (
                <SelectItem key={e} value={e} className="text-[11px]">
                  {e}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <span className="truncate text-[10px] text-muted-foreground" title={activeLeg?.symbol}>
            {activeLeg?.symbol ?? '…'}
          </span>
        )}

        <div className="ml-auto flex items-center gap-1">
          {undTick?.ltp != null && (
            <span className="font-mono text-[10px] font-semibold tabular-nums text-foreground">
              {underlying}{' '}
              <span className={(undTick.change_percent ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>
                {fmtNum(undTick.ltp)} {(undTick.change_percent ?? 0) >= 0 ? '+' : ''}
                {(undTick.change_percent ?? 0).toFixed(2)}%
              </span>
            </span>
          )}
          <span className={cn('rounded px-1 py-px text-[9px] font-bold', wsBadge.cls)}>{wsBadge.label}</span>
          <button
            type="button"
            onClick={() => setStrikeCount((n) => (n >= 10 ? 4 : n + 2))}
            className="rounded border border-border px-1 py-px text-[9px] font-semibold text-muted-foreground hover:text-foreground"
            title="Strikes shown per side"
          >
            ×{strikeCount}
          </button>
          <button
            type="button"
            onClick={() => setFollowChart((v) => !v)}
            className={cn(
              'rounded border px-1 py-px text-[9px] font-semibold',
              followChart
                ? 'border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400'
                : 'border-border text-muted-foreground'
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

      {/* Sync flash + chain loading */}
      {flash && (
        <div className="border-b bg-sky-500/10 px-2 py-0.5 text-[10px] font-medium text-sky-600 dark:text-sky-400">
          ⚡ {flash}
        </div>
      )}

      {/* Chain ladder with per-strike depth */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {optionsMode && chain.length > 0 ? (
          <table className="w-full border-collapse text-[10px]">
            <thead className="sticky top-0 z-10 bg-background/95 text-[9px] uppercase tracking-wide text-muted-foreground backdrop-blur">
              <tr className="border-b">
                <th className="px-1.5 py-0.5 text-right font-medium">CE B/A</th>
                <th className="px-1.5 py-0.5 text-right font-medium">CE LTP</th>
                <th className="px-1.5 py-0.5 text-center font-medium">Strike</th>
                <th className="px-1.5 py-0.5 text-left font-medium">PE LTP</th>
                <th className="px-1.5 py-0.5 text-left font-medium">PE B/A</th>
              </tr>
            </thead>
            <tbody>
              {chain.map((r) => {
                const isAtm = r.strike === chainResp?.atm_strike
                const ceSel = String(r.strike) === ceStrike
                const peSel = String(r.strike) === peStrike
                const ceRow = getTick(r.ce.symbol ?? '', foExchange)
                const peRow = getTick(r.pe.symbol ?? '', foExchange)
                return (
                  <tr key={r.strike} className={cn('border-b border-border/40', isAtm && 'bg-primary/5')}>
                    {/* CE: depth cell (bid/ask poll) then live LTP; click selects the CE strike */}
                    <td className="px-1 py-0.5 text-right">
                      {depthOn && r.ce.symbol ? (
                        <StrikeDepthCell apiKey={apiKey} symbol={r.ce.symbol} exchange={foExchange} pollMs={5000} />
                      ) : (
                        <span className="text-[9px] text-muted-foreground">—</span>
                      )}
                    </td>
                    <td
                      className={cn(
                        'cursor-pointer px-1.5 py-0.5 text-right font-mono tabular-nums hover:bg-accent',
                        ceSel && 'bg-emerald-500/10 font-bold text-emerald-700 dark:text-emerald-400'
                      )}
                      onClick={() => setCeStrike(String(r.strike))}
                      title={r.ce.symbol ?? ''}
                    >
                      {ceRow?.ltp != null ? ceRow.ltp.toFixed(dec) : r.ce.ltp != null ? r.ce.ltp.toFixed(dec) : '—'}
                      {ceRow?.change_percent != null && (
                        <span className={cn('ml-1 text-[8px]', ceRow.change_percent >= 0 ? 'text-emerald-600/80 dark:text-emerald-400/80' : 'text-rose-600/80 dark:text-rose-400/80')}>
                          {ceRow.change_percent >= 0 ? '+' : ''}{ceRow.change_percent.toFixed(1)}%
                        </span>
                      )}
                    </td>
                    <td className={cn('px-1.5 py-0.5 text-center font-mono tabular-nums', isAtm ? 'font-bold text-primary' : 'text-foreground/80')}>
                      {r.strike.toLocaleString('en-IN')}
                    </td>
                    <td
                      className={cn(
                        'cursor-pointer px-1.5 py-0.5 text-left font-mono tabular-nums hover:bg-accent',
                        peSel && 'bg-rose-500/10 font-bold text-rose-700 dark:text-rose-400'
                      )}
                      onClick={() => setPeStrike(String(r.strike))}
                      title={r.pe.symbol ?? ''}
                    >
                      {peRow?.change_percent != null && (
                        <span className={cn('mr-1 text-[8px]', peRow.change_percent >= 0 ? 'text-emerald-600/80 dark:text-emerald-400/80' : 'text-rose-600/80 dark:text-rose-400/80')}>
                          {peRow.change_percent >= 0 ? '+' : ''}{peRow.change_percent.toFixed(1)}%
                        </span>
                      )}
                      {peRow?.ltp != null ? peRow.ltp.toFixed(dec) : r.pe.ltp != null ? r.pe.ltp.toFixed(dec) : '—'}
                    </td>
                    <td className="px-1 py-0.5 text-left">
                      {depthOn && r.pe.symbol ? (
                        <StrikeDepthCell apiKey={apiKey} symbol={r.pe.symbol} exchange={foExchange} pollMs={5000} />
                      ) : (
                        <span className="text-[9px] text-muted-foreground">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : !optionsMode ? (
          <div className="p-3 text-center text-[11px] text-muted-foreground">
            {futInstrument ? (
              <span className="font-mono">
                {futInstrument.symbol} · lot {futInstrument.lotsize}
                {futTick?.ltp != null && <> · LTP {fmtNum(futTick.ltp)}</>}
              </span>
            ) : (
              'Loading futures…'
            )}
          </div>
        ) : (
          <div className="p-3 text-center text-[11px] text-muted-foreground">
            {chainLoading ? 'Loading chain…' : 'No chain — pick an underlying & expiry'}
          </div>
        )}
      </div>

      {/* Order strip */}
      <div className="flex flex-wrap items-center gap-1.5 border-t bg-muted/20 px-2 py-1">
        <label className="flex items-center gap-1">
          <span
            className={cn(
              'rounded px-1.5 py-0.5 text-[9px] font-bold',
              armed ? 'bg-rose-500/15 text-rose-600 dark:text-rose-400' : 'bg-muted text-muted-foreground'
            )}
            title={armed ? 'One-Click armed (top bar)' : 'One-Click off — enable in the top bar'}
          >
            {armed ? 'ARMED' : 'SAFE'}
          </span>
        </label>
        <div className="flex items-center gap-0.5" title={`Lot size ${ceLeg?.lotsize ?? activeLeg?.lotsize ?? '—'}`}>
          <button type="button" onClick={() => setLots((n) => Math.max(1, n - 1))} className="rounded border px-1.5 text-[10px] hover:bg-accent">−</button>
          <span className="w-5 text-center font-mono text-[10px] tabular-nums">{lots}</span>
          <button type="button" onClick={() => setLots((n) => Math.min(MAX_LOTS, n + 1))} className="rounded border px-1.5 text-[10px] hover:bg-accent">+</button>
        </div>
        <Select value={product} onValueChange={(v) => setProduct(v as ScalpingProduct)}>
          <SelectTrigger className="h-5 w-[62px] border-0 bg-transparent px-1 text-[10px] shadow-none focus:ring-0" aria-label="Product">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="MIS" className="text-[11px]">MIS</SelectItem>
            <SelectItem value="NRML" className="text-[11px]">NRML</SelectItem>
          </SelectContent>
        </Select>

        {optionsMode ? (
          <div className="grid flex-1 grid-cols-4 gap-1">
            <Button disabled={!ceLeg} onClick={() => submitOrder(ceLeg, 'BUY')} className="h-6 bg-emerald-600 px-1 text-[10px] font-bold hover:bg-emerald-700">
              B CE
            </Button>
            <Button disabled={!ceLeg} onClick={() => submitOrder(ceLeg, 'SELL')} className="h-6 bg-rose-600 px-1 text-[10px] font-bold hover:bg-rose-700">
              S CE
            </Button>
            <Button disabled={!peLeg} onClick={() => submitOrder(peLeg, 'BUY')} className="h-6 bg-emerald-600 px-1 text-[10px] font-bold hover:bg-emerald-700">
              B PE
            </Button>
            <Button disabled={!peLeg} onClick={() => submitOrder(peLeg, 'SELL')} className="h-6 bg-rose-600 px-1 text-[10px] font-bold hover:bg-rose-700">
              S PE
            </Button>
          </div>
        ) : (
          <div className="grid flex-1 grid-cols-2 gap-1">
            <Button disabled={!activeLeg} onClick={() => submitOrder(activeLeg, 'BUY')} className="h-6 bg-emerald-600 px-1 text-[10px] font-bold hover:bg-emerald-700">
              Buy
            </Button>
            <Button disabled={!activeLeg} onClick={() => submitOrder(activeLeg, 'SELL')} className="h-6 bg-rose-600 px-1 text-[10px] font-bold hover:bg-rose-700">
              Sell
            </Button>
          </div>
        )}

        <div className="flex items-center gap-1 font-mono text-[10px] tabular-nums">
          <span className="text-muted-foreground">Net</span>
          <span className={cn('font-semibold', openMtm.qty === 0 && 'text-muted-foreground')}>{openMtm.qty}</span>
          <span className={cn('font-semibold', openMtm.mtm >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
            {openMtm.mtm >= 0 ? '+' : ''}{openMtm.mtm.toFixed(1)}
          </span>
        </div>
        <Button variant="outline" size="sm" className="h-5 px-1.5 text-[9px]" onClick={doCloseAll} title="Flatten every scalped position (F6 on /scalping)">
          Close All
        </Button>
        <Button variant="outline" size="sm" className="h-5 px-1.5 text-[9px]" onClick={doCancelAll} title="Cancel all open orders">
          Cancel
        </Button>
        <button
          type="button"
          onClick={() => setDepthOn((v) => !v)}
          className={cn(
            'rounded border px-1 py-px text-[9px] font-semibold',
            depthOn ? 'border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400' : 'border-border text-muted-foreground'
          )}
          title="Per-strike best bid/ask (REST depth poll)"
        >
          D
        </button>
        <span className="text-[8px] text-muted-foreground">
          {ceLeg?.symbol && <span className="mr-1">CE {ceTick?.ltp != null ? ceTick.ltp.toFixed(dec) : '—'}</span>}
          {peLeg?.symbol && <span>PE {peTick?.ltp != null ? peTick.ltp.toFixed(dec) : '—'}</span>}
        </span>
      </div>
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
    queryFn: () => scalpingApi.getAllUnderlyings(exchange, 'options'),
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
