/**
 * The watchlist panel on the right of the charting terminal.
 *
 * Lists live in SQLite (blueprints/watchlist.py), not in localStorage: a list
 * built up over months is real work, and in the browser it dies with a cache
 * clear and does not follow the user to a second device.
 *
 * Prices come from useLivePrice, the same hook Positions and Holdings use. It
 * streams over the app's shared WebSocket connection, falls back to the
 * multiquote REST API when the feed is unavailable or the market is closed,
 * and pauses both when the tab is hidden. Reusing it means the watchlist adds
 * no second socket and no polling loop of its own.
 */

import {
  ChevronDown,
  FolderPlus,
  GripVertical,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  type GlobalQuote,
  type Watchlist,
  type WatchlistItem,
  watchlistApi,
  watchlistError,
} from '@/api/watchlist'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { type PriceableItem, useLivePrice } from '@/hooks/useLivePrice'
import { useMarketStatus } from '@/hooks/useMarketStatus'
import { needsPreviousClose, previousClose } from '@/lib/trading/previousClose'
import { setSyncSymbol } from '@/lib/scalperSync'
import type { SearchRow } from '@/lib/trading/terminal'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'
import { PANEL_HEADER, PanelShell } from './panelShell'
import { SymbolSearchDialog } from './SymbolSearchDialog'

/** Remembers which list was open, so a reload lands where the user left off. */
const ACTIVE_LIST_KEY = 'oa-trading-watchlist'

/**
 * Global (dollar) instruments the panel can show alongside exchange rows.
 *
 * They are stored in the same lists with exchange GLOBAL — which no Indian
 * master contract carries — and priced from TradingView by the backend. The
 * catalog here must stay in step with services/global_quotes_service.py.
 */
const GLOBAL_KEYS = ['USOIL', 'BRENT', 'GOLD', 'SILVER', 'NATGAS', 'GIFTNIFTY'] as const
const GLOBAL_POLL_MS = 5000

/** Created on first open so the panel is never an empty shell with no list. */
const DEFAULT_LIST_NAME = 'Watchlist'

/**
 * How often the multiquote snapshot is refreshed.
 *
 * Not a fallback despite the option's name: useLivePrice polls this
 * unconditionally, and only the choice of which LTP to display prefers the
 * socket. The one thing this panel actually needs from the snapshot is
 * `prev_close`, which changes once a day, so a rate faster than the hook's
 * own 30s default would be paying the broker for nothing.
 */
const SNAPSHOT_REFRESH_MS = 60000

interface Props {
  /** Authenticates the daily-history lookup that resolves previous closes. */
  apiKey: string
  /** Loads a row into whichever pane the user last touched. */
  onPick(row: SearchRow): void
  /** Bound to the focused pane's search, so results are broker-supported. */
  search(query: string, exchange?: string, limit?: number): Promise<SearchRow[]>
  /** Highlights the row matching the focused pane, as `EXCHANGE:SYMBOL`. */
  activeSymbol?: string | null
}

interface Quote {
  ltp: number
  /**
   * Null when the snapshot carries no previous close, which is not the same
   * as unchanged. Collapsing the two rendered a confident green +0.00% for a
   * pre-open or thinly quoted instrument, indistinguishable from a symbol
   * that genuinely had not moved.
   */
  change: number | null
  changePercent: number | null
  volume?: number
  high?: number
  low?: number
  open?: number
  /** Set on GLOBAL rows, whose prices are dollar-denominated. */
  currency?: 'USD'
}

/**
 * Every column the panel can show, in the order they appear.
 *
 * Which of them are on is the user's choice, so the row and its header both
 * build their grid from the same selection: a template computed in one place
 * cannot fall out of register with the cells it sizes.
 *
 * `tone` marks a column that carries direction, which is the only reason a
 * number here is ever coloured.
 */
interface Column {
  id: string
  label: string
  width: number
  /** Carries direction, which is the only reason a number here is coloured. */
  tone?: boolean
  get(quote: Quote): string
}

const COLUMNS: readonly Column[] = [
  { id: 'last', label: 'Last', width: 64, get: (q: Quote) => (q.currency === 'USD' ? `$${fmt(q.ltp)}` : fmt(q.ltp)) },
  {
    id: 'change',
    label: 'Chg',
    width: 60,
    tone: true,
    get: (q: Quote) =>
      q.change == null
        ? '-'
        : `${q.change >= 0 ? '+' : ''}${q.currency === 'USD' ? '$' : ''}${fmt(q.change)}`,
  },
  {
    id: 'changePercent',
    label: 'Chg%',
    width: 58,
    tone: true,
    get: (q: Quote) =>
      q.changePercent == null
        ? '-'
        : `${q.changePercent >= 0 ? '+' : ''}${q.changePercent.toFixed(2)}%`,
  },
  { id: 'volume', label: 'Vol', width: 58, get: (q: Quote) => compact(q.volume) },
  { id: 'high', label: 'High', width: 60, get: (q: Quote) => dollarOr(q.high, q) },
  { id: 'low', label: 'Low', width: 60, get: (q: Quote) => dollarOr(q.low, q) },
  { id: 'open', label: 'Open', width: 60, get: (q: Quote) => dollarOr(q.open, q) },
] as const
type ColumnId = string

/** Lakhs and crores: raw volume does not fit a 58px column. */
function compact(value: number | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return '-'
  if (value >= 1e7) return `${(value / 1e7).toFixed(2)}Cr`
  if (value >= 1e5) return `${(value / 1e5).toFixed(2)}L`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return String(Math.round(value))
}

/** What the panel shows, per user. */
interface Display {
  columns: ColumnId[]
  logo: boolean
  exchange: boolean
  /** Group rows under named section headers. */
  sections: boolean
}

const DISPLAY_KEY = 'oa-trading-watchlist-display'
const DISPLAY_DEFAULT: Display = {
  columns: ['last', 'changePercent'],
  logo: true,
  exchange: true,
  sections: true,
}

function readDisplay(): Display {
  try {
    const saved = JSON.parse(localStorage.getItem(DISPLAY_KEY) || '{}')
    const ids = COLUMNS.map((c) => c.id) as string[]
    return {
      // Filtered against the live column list, so a stored id from an older
      // build cannot leave the header and the rows disagreeing.
      columns: Array.isArray(saved.columns)
        ? (saved.columns.filter((c: unknown) => ids.includes(c as string)) as ColumnId[])
        : DISPLAY_DEFAULT.columns,
      logo: typeof saved.logo === 'boolean' ? saved.logo : true,
      exchange: typeof saved.exchange === 'boolean' ? saved.exchange : true,
      sections: typeof saved.sections === 'boolean' ? saved.sections : true,
    }
  } catch {
    return DISPLAY_DEFAULT
  }
}

export function itemSection(item: WatchlistItem): string {
  if (item.section && item.section.trim().length > 0) return item.section.trim()
  const exch = (item.exchange || '').toUpperCase()
  if (exch === 'GLOBAL') return 'Global'
  if (exch === 'NSE_INDEX' || exch === 'BSE_INDEX') return 'Indices'
  if (exch === 'MCX') return 'Commodities'
  if (exch === 'CDS') return 'Currency'
  if (exch === 'NSE' || exch === 'BSE') return 'Equities'
  return 'Symbols'
}

/** High/low/open: a dollar cell when the row is global, a dash when absent. */
function dollarOr(value: number | null | undefined, q: Quote): string {
  if (!value) return '-'
  return q.currency === 'USD' ? `$${fmt(value)}` : fmt(value)
}

/**
 * Compact price.
 *
 * One decimal above a thousand so a five-figure index still fits the column,
 * two below it. Both bounds move together: toLocaleString throws a RangeError
 * if the minimum is ever left above the maximum, which is a throw inside
 * render, and the app's root error boundary turns that into a blank page.
 */
function fmt(value: number): string {
  if (!Number.isFinite(value)) return '-'
  const dp = Math.abs(value) >= 1000 ? 1 : 2
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  })
}

/**
 * A stable colour per instrument.
 *
 * A tinted initial, not a brand logo: we have no logo artwork, and inventing
 * one would misrepresent the company. What it buys is a fixed anchor at the
 * start of every row, so the eye finds a symbol by shape and position rather
 * than reading down a column of similar words. The hue is derived from the
 * symbol itself, so it never changes between sessions or devices.
 */
function symbolTint(symbol: string): string {
  let hash = 0
  for (let i = 0; i < symbol.length; i++) hash = (hash * 31 + symbol.charCodeAt(i)) | 0
  return `hsl(${Math.abs(hash) % 360} 55% 45%)`
}

/** The row and its column header share this, so the two cannot drift apart. */
/** The row and its header share this, so the two cannot drift apart. */
function rowGrid(columns: typeof COLUMNS): string {
  // 1fr for the symbol, each chosen column at its own width, 16px for the
  // remove control.
  return `1fr ${columns.map((c) => `${c.width}px`).join(' ')} 16px`
}

export function WatchlistPanel({ apiKey, onPick, search, activeSymbol }: Props) {
  const [lists, setLists] = useState<Watchlist[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [dragId, setDragId] = useState<number | null>(null)
  const [overId, setOverId] = useState<number | null>(null)
  /**
   * Where the row lands relative to the hovered one. dropOn splices the row
   * out before re-inserting it, so a downward drag naturally lands BELOW the
   * target; tracking the side here is what draws the line and inserts on the
   * side the user actually sees.
   */
  const [dropAfterId, setDropAfterId] = useState(false)
  /**
   * The section header the dragged row is over. Undefined is "over no
   * header" — kept apart from null, which is the default (unnamed) group's
   * header, so it does not light up while the row is over other rows.
   */
  const [overSection, setOverSection] = useState<string | null | undefined>(undefined)

  /** One dialog drives create, rename and copy; `mode` says which. */
  const [nameDialog, setNameDialog] = useState<{
    mode: 'create' | 'rename' | 'copy'
    value: string
  } | null>(null)
  const [confirm, setConfirm] = useState<{ kind: 'delete' | 'clear'; name: string } | null>(null)
  /** "New section" flow: names a group, then files the chosen row into it. */
  const [sectionDialog, setSectionDialog] = useState<{ item?: WatchlistItem | null } | null>(null)
  const [sectionName, setSectionName] = useState('')

  const fileRef = useRef<HTMLInputElement>(null)
  const { isMarketOpen } = useMarketStatus()

  const [display, setDisplay] = useState<Display>(readDisplay)
  useEffect(() => {
    localStorage.setItem(DISPLAY_KEY, JSON.stringify(display))
  }, [display])

  /** The chosen columns, in the canonical order rather than click order. */
  const shownColumns = useMemo(
    () => COLUMNS.filter((c) => display.columns.includes(c.id)),
    [display.columns]
  )
  /**
   * The narrowest the panel can be drawn at with these columns chosen.
   *
   * 16px of padding, 16px for the remove control, 6px between each cell, the
   * columns themselves, and 96px left for the symbol -- enough for a
   * BANKNIFTY and its exchange tag. Without a floor the symbol column
   * absorbed every column added and collapsed to two letters.
   */
  const minWidth = useMemo(
    () =>
      96 +
      16 +
      16 +
      shownColumns.reduce((sum, c) => sum + c.width, 0) +
      (shownColumns.length + 1) * 6,
    [shownColumns]
  )

  const gridTemplate = useMemo(
    () => rowGrid(shownColumns as unknown as typeof COLUMNS),
    [shownColumns]
  )

  const toggleColumn = (id: ColumnId) =>
    setDisplay((prev) => ({
      ...prev,
      columns: prev.columns.includes(id)
        ? prev.columns.filter((c) => c !== id)
        : [...prev.columns, id],
    }))

  const active = useMemo(
    () => lists.find((l) => l.id === activeId) ?? lists[0] ?? null,
    [lists, activeId]
  )

  /* ── loading ──────────────────────────────────────────────────────────── */
  const loadLists = useCallback(async (): Promise<Watchlist[] | null> => {
    try {
      let loaded = await watchlistApi.list()
      if (loaded.length === 0) {
        // A panel with no list at all has no useful empty state: every action
        // in it needs a list to act on. Making one is what the user would do
        // first anyway.
        try {
          loaded = [await watchlistApi.create(DEFAULT_LIST_NAME)]
        } catch {
          // Someone got there first, and the name is unique per user, so this
          // is a 409 rather than a failure. StrictMode double-mounts this
          // effect in development and a second tab does the same in
          // production, both of which raced two creates on a fresh install
          // and showed the loser an error on a working setup.
          loaded = await watchlistApi.list()
          if (loaded.length === 0) throw new Error('Could not create a watchlist')
        }
      }
      setLoadError(null)
      return loaded
    } catch (error) {
      // Recorded rather than only toasted. Without this the panel falls through
      // to the empty state and tells the user their list is empty when the
      // truth is that the request failed, offering a button that cannot work.
      setLoadError(watchlistError(error, 'Could not load your watchlists'))
      return null
    }
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      const loaded = await loadLists()
      if (!alive) return
      if (loaded) {
        setLists(loaded)
        const saved = Number(localStorage.getItem(ACTIVE_LIST_KEY))
        setActiveId(loaded.some((l) => l.id === saved) ? saved : loaded[0].id)
      }
      setLoading(false)
    })()
    return () => {
      alive = false
    }
  }, [loadLists])

  useEffect(() => {
    if (activeId != null) localStorage.setItem(ACTIVE_LIST_KEY, String(activeId))
  }, [activeId])

  const refresh = useCallback(
    async (selectId?: number) => {
      const loaded = await watchlistApi.list()
      setLists(loaded)
      if (selectId != null) setActiveId(selectId)
      else if (loaded.length && !loaded.some((l) => l.id === activeId)) setActiveId(loaded[0].id)
    },
    [activeId]
  )

  const retry = async () => {
    setLoading(true)
    const loaded = await loadLists()
    if (loaded) {
      setLists(loaded)
      // Keep the list the user was on. Jumping to the first one made a
      // recovered failure look like a lost selection.
      const saved = Number(localStorage.getItem(ACTIVE_LIST_KEY))
      setActiveId(loaded.some((l) => l.id === saved) ? saved : loaded[0].id)
    }
    setLoading(false)
  }

  /* ── live prices ──────────────────────────────────────────────────────── */
  const items = useMemo(() => active?.items ?? [], [active])

  // A stable identity for the hook's dependency chain: `items` is a fresh array
  // on every render, and useLivePrice keys its subscription off this.
  const symbolKey = items.map((i) => `${i.exchange}:${i.symbol}`).join(',')
  // biome-ignore lint/correctness/useExhaustiveDependencies: symbolKey IS the identity of items; depending on the array itself resubscribes every render
  const priceable = useMemo<PriceableItem[]>(
    // GLOBAL rows are priced from TradingView via the panel's own poll, not
    // the broker feed — subscribing them would only produce dead rows.
    () =>
      items
        .filter((i) => i.exchange.toUpperCase() !== 'GLOBAL')
        .map((i) => ({ symbol: i.symbol, exchange: i.exchange })),
    [symbolKey]
  )

  const {
    data: priced,
    multiQuotes,
    isLive,
    isFallbackMode,
    isAnyMarketOpen,
  } = useLivePrice(priceable, {
    enabled: priceable.length > 0,
    useMultiQuotesFallback: true,
    multiQuotesRefreshInterval: SNAPSHOT_REFRESH_MS,
    pauseWhenHidden: true,
  })

  /**
   * Previous closes resolved from daily history.
   *
   * Only for instruments whose quote did not carry a usable one. What a
   * broker puts in `prev_close` is its own convention, and some report the
   * CURRENT session's close there, which equals the last traded price and
   * turns every row into +0.00%. See lib/trading/previousClose.ts.
   */
  const [resolvedCloses, setResolvedCloses] = useState<Record<string, number>>({})

  /* ── global (dollar) rows ─────────────────────────────────────────────── */
  const hasGlobals = useMemo(
    () => items.some((i) => i.exchange.toUpperCase() === 'GLOBAL'),
    [items]
  )
  const [globalQuotes, setGlobalQuotes] = useState<Record<string, GlobalQuote>>({})

  useEffect(() => {
    if (!hasGlobals) return
    let alive = true
    const load = async () => {
      try {
        const rows = await watchlistApi.globalQuotes()
        if (alive) setGlobalQuotes(rows)
      } catch {
        // Quiet: the next poll retries, and globals moving slowly means a
        // missed cycle leaves the last prices on the rows.
      }
    }
    void load()
    const t = setInterval(load, GLOBAL_POLL_MS)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [hasGlobals])

  /**
   * Last price and change per row.
   *
   * The live price comes from the hook, which has already chosen between the
   * feed and the REST fallback. The previous close comes from the snapshot
   * when it is usable and from daily history when it is not, which is the
   * same bar the chart legend reads, so a row agrees with the chart it opens.
   */
  const quotes = useMemo(() => {
    const next: Record<string, Quote> = {}
    for (const row of priced) {
      const key = `${row.exchange}:${row.symbol}`
      // GLOBAL rows never reach the broker feed; their prices arrive from
      // watchlistApi.globalQuotes below, not from this loop.
      if (row.exchange.toUpperCase() === 'GLOBAL') continue
      const snapshot = multiQuotes.get(key)
      const ltp = row.ltp ?? snapshot?.ltp
      if (typeof ltp !== 'number' || ltp === 0) continue

      const prevClose = needsPreviousClose(snapshot?.prev_close, ltp)
        ? resolvedCloses[key]
        : snapshot?.prev_close
      const known = typeof prevClose === 'number' && prevClose > 0
      next[key] = {
        ltp,
        change: known ? ltp - (prevClose as number) : null,
        changePercent: known ? ((ltp - (prevClose as number)) / (prevClose as number)) * 100 : null,
        volume: snapshot?.volume,
        high: snapshot?.high,
        low: snapshot?.low,
        open: snapshot?.open,
      }
    }
    // Dollar rows ride the same Quote shape, so the columns below cannot
    // tell them apart — except that change/changePercent come straight from
    // the source (it reports its own previous close).
    for (const gk of GLOBAL_KEYS) {
      const g = globalQuotes[gk]
      if (!g || typeof g.ltp !== 'number') continue
      next[`GLOBAL:${gk}`] = {
        ltp: g.ltp,
        change: g.ch,
        changePercent: g.chp,
        volume: undefined,
        high: g.high ?? undefined,
        low: g.low ?? undefined,
        open: g.open ?? undefined,
      }
    }
    return next
  }, [priced, multiQuotes, resolvedCloses, globalQuotes])

  // One request per instrument per trading day, and only for the ones that
  // need it, so a broker whose quote already carries a real previous close
  // costs nothing extra.
  useEffect(() => {
    let alive = true
    for (const row of priced) {
      const key = `${row.exchange}:${row.symbol}`
      const snapshot = multiQuotes.get(key)
      const ltp = row.ltp ?? snapshot?.ltp
      if (typeof ltp !== 'number' || ltp === 0) continue
      if (!needsPreviousClose(snapshot?.prev_close, ltp)) continue
      if (resolvedCloses[key] !== undefined) continue

      void previousClose(apiKey, row.symbol, row.exchange, isMarketOpen(row.exchange)).then(
        (value) => {
          // A failure is recorded as 0, not skipped. This effect re-runs on
          // every tick (useLivePrice hands back a new array each time), and an
          // absent key reads as "not asked yet", so leaving it out re-requested
          // an unresolvable symbol several times a second.
          if (alive) setResolvedCloses((prev) => ({ ...prev, [key]: value ?? 0 }))
        }
      )
    }
    return () => {
      alive = false
    }
  }, [priced, multiQuotes, resolvedCloses, apiKey, isMarketOpen])

  /**
   * When the snapshot last actually arrived.
   *
   * useLivePrice swallows a failed multiquote silently, so "refreshing over
   * REST" was a statement of intent rather than outcome: with both the socket
   * and REST down the caption still claimed a refresh while nothing moved.
   * Watching the map's identity is what turns that into an observation.
   */
  const [lastSnapshotAt, setLastSnapshotAt] = useState<number | null>(null)
  // biome-ignore lint/correctness/useExhaustiveDependencies: multiQuotes is the signal; its identity changes only when a fetch actually resolved
  useEffect(() => {
    if (multiQuotes.size > 0) setLastSnapshotAt(Date.now())
  }, [multiQuotes])

  /**
   * What the footer says about where these numbers came from.
   *
   * Null while the feed is carrying ticks, which is the case that needs no
   * caption.
   */
  const stale = lastSnapshotAt != null && Date.now() - lastSnapshotAt > SNAPSHOT_REFRESH_MS * 2
  const feedNote =
    priceable.length === 0 || isLive
      ? null
      : !isAnyMarketOpen
        ? 'Market closed. Showing last traded prices.'
        : !isFallbackMode
          ? null
          : stale && lastSnapshotAt
            ? `Not updating. Last updated ${new Date(lastSnapshotAt).toLocaleTimeString()}`
            : 'Live feed unavailable. Refreshing over REST.'

  /* ── list actions ─────────────────────────────────────────────────────── */
  const submitName = async () => {
    if (!nameDialog) return
    const name = nameDialog.value.trim()
    if (!name) return

    try {
      if (nameDialog.mode === 'rename' && active) {
        await watchlistApi.rename(active.id, name)
        await refresh(active.id)
      } else {
        // Create and copy are the same call; a copy just carries the current
        // list's instruments with it.
        const seed = nameDialog.mode === 'copy' ? active?.items : undefined
        const created = await watchlistApi.create(name, seed)
        await refresh(created.id)
      }
      setNameDialog(null)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not save the list'))
    }
  }

  const runConfirm = async () => {
    if (!confirm || !active) return
    try {
      if (confirm.kind === 'delete') {
        await watchlistApi.remove(active.id)
        const remaining = lists.filter((l) => l.id !== active.id)
        // Deleting the last list would leave the panel with nothing to act on,
        // so it is replaced rather than left empty.
        if (remaining.length === 0) {
          const created = await watchlistApi.create(DEFAULT_LIST_NAME)
          await refresh(created.id)
        } else {
          await refresh(remaining[0].id)
        }
      } else {
        await watchlistApi.clear(active.id)
        await refresh(active.id)
      }
      setConfirm(null)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not update the list'))
    }
  }

  const addSymbol = async (row: SearchRow) => {
    if (!active) return
    try {
      await watchlistApi.addItem(active.id, row.symbol, row.exchange)
      await refresh(active.id)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not add the instrument'))
    }
  }

  const removeSymbol = async (item: WatchlistItem) => {
    if (!active) return
    // Optimistic: the row disappears on click rather than after a round trip,
    // and refresh() below is what makes it authoritative.
    setLists((prev) =>
      prev.map((l) =>
        l.id === active.id ? { ...l, items: l.items.filter((i) => i.id !== item.id) } : l
      )
    )
    try {
      await watchlistApi.removeItem(active.id, item.id)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not remove the instrument'))
      // The recovery read can fail too, and this runs inside a void-called
      // async function, where that would surface as an unhandled rejection.
      await refresh(active.id).catch(() => {})
    }
  }

  /* ── drag to reorder ──────────────────────────────────────────────────── */
  const dropOn = async (targetId: number) => {
    setOverId(null)
    setOverSection(undefined)
    if (!active || dragId == null || dragId === targetId) return
    const after = dropAfterId
    setDropAfterId(false)

    const ordered = [...active.items]
    const from = ordered.findIndex((i) => i.id === dragId)
    if (from < 0 || !ordered.some((i) => i.id === targetId)) return

    // Splice out, then re-insert on the side of the target the user saw.
    const [moved] = ordered.splice(from, 1)
    const insertAt = ordered.findIndex((i) => i.id === targetId) + (after ? 1 : 0)
    ordered.splice(insertAt, 0, moved)

    // Landing between two rows of one section files the row under it: the
    // stored order groups the display, so crossing a boundary IS moving
    // between sections. Sections off, the row keeps whatever it had.
    let section = moved.section ?? null
    if (display.sections) {
      const above = insertAt > 0 ? ordered[insertAt - 1] : undefined
      const below = ordered[insertAt + 1]
      section = above ? itemSection(above) : below ? itemSection(below) : null
    }
    const next = display.sections ? { ...moved, section } : moved
    const withMoved = ordered.map((i) => (i.id === next.id ? next : i))

    setLists((prev) => prev.map((l) => (l.id === active.id ? { ...l, items: withMoved } : l)))
    setDragId(null)

    try {
      await watchlistApi.reorderItems(
        active.id,
        withMoved.map((i) => i.id)
      )
      if (display.sections && (next.section ?? null) !== (moved.section ?? null)) {
        await watchlistApi.setItemSection(next.id, next.section ?? null)
      }
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not save the new order'))
      await refresh(active.id).catch(() => {})
    }
  }

  /**
   * Drop the dragged row straight onto a section header — the fastest way to
   * file a row without hunting for the boundary between two groups.
   */
  const dropOnSection = async (section: string | null) => {
    setOverId(null)
    setOverSection(undefined)
    setDropAfterId(false)
    if (!active || dragId == null) return
    const item = active.items.find((i) => i.id === dragId)
    setDragId(null)
    if (!item || (item.section ?? null) === section) return

    setLists((prev) =>
      prev.map((l) =>
        l.id === active.id
          ? { ...l, items: l.items.map((i) => (i.id === item.id ? { ...i, section } : i)) }
          : l
      )
    )
    try {
      await watchlistApi.setItemSection(item.id, section)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not move the instrument'))
      await refresh(active.id).catch(() => {})
    }
  }

  /** File a row under `section` from the row menu — same persistence as the
   * drag path, but reachable without a drag: touch devices, keyboard users
   * and anyone who finds dropping on a header fiddly. Also used right after
   * "New section" creates a group for the row being filed. */
  const moveItemToSection = async (item: WatchlistItem, section: string | null) => {
    if (!active || (item.section ?? null) === section) return
    // Optimistic like the rest of the panel: re-file locally, persist, roll
    // back on failure by refreshing.
    setLists((prev) =>
      prev.map((l) =>
        l.id === active.id
          ? { ...l, items: l.items.map((i) => (i.id === item.id ? { ...i, section } : i)) }
          : l
      )
    )
    try {
      await watchlistApi.setItemSection(item.id, section)
    } catch (error) {
      showToast.error(watchlistError(error, 'Could not move the instrument'))
      await refresh(active.id).catch(() => {})
    }
  }

  /** Distinct section names in the active list, in stored order — the row
   * menu's filing targets. Unnamed rows are the "(no section)" target. */
  const sectionNames = useMemo(() => {
    const seen: string[] = []
    for (const i of items) {
      const name = i.section?.trim()
      if (name && !seen.includes(name)) seen.push(name)
    }
    return seen
  }, [items])

  /** Create a section from the dialog and file the triggering row into it.
   * Moving the row to the END of the list under its new name keeps the
   * stored order grouped (the display is a fold over stored order). */
  const submitNewSection = async () => {
    const name = sectionName.trim()
    const item = sectionDialog?.item
    setSectionDialog(null)
    setSectionName('')
    if (!name || !active) return
    setDisplay((prev) => ({ ...prev, sections: true }))
    if (item) {
      // Re-order locally: pull the row to the end, then set its section.
      const ordered = [...active.items.filter((i) => i.id !== item.id), { ...item, section: name }]
      setLists((prev) => prev.map((l) => (l.id === active.id ? { ...l, items: ordered } : l)))
      try {
        await watchlistApi.reorderItems(
          active.id,
          ordered.map((i) => i.id)
        )
        await watchlistApi.setItemSection(item.id, name)
      } catch (error) {
        showToast.error(watchlistError(error, 'Could not create the section'))
        await refresh(active.id).catch(() => {})
      }
    } else {
      showToast.success(`Section "${name}" ready — drag symbols into it`)
    }
  }

  /* ── import / export ──────────────────────────────────────────────────── */
  const exportList = () => {
    if (!active) return
    const payload = {
      name: active.name,
      items: active.items.map((i) => ({ symbol: i.symbol, exchange: i.exchange })),
    }
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `${active.name.replace(/[^\w.-]+/g, '_')}.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  const importList = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text())
      const raw = Array.isArray(parsed) ? parsed : parsed.items
      if (!Array.isArray(raw)) throw new Error('No instruments in that file')

      const seed = raw
        .map((entry: { symbol?: string; exchange?: string }) => ({
          symbol: String(entry?.symbol ?? '')
            .trim()
            .toUpperCase(),
          exchange: String(entry?.exchange ?? '')
            .trim()
            .toUpperCase(),
        }))
        .filter((entry) => entry.symbol && entry.exchange)
      if (seed.length === 0) throw new Error('No instruments in that file')

      // The name in the file may already be taken. Suffixing on the client is
      // what keeps an import from failing on a 409 the user cannot resolve
      // without editing the file by hand.
      const base = String(parsed?.name || file.name.replace(/\.json$/i, '')) || 'Imported'
      const taken = new Set(lists.map((l) => l.name))
      let name = base
      for (let n = 2; taken.has(name); n++) name = `${base} (${n})`

      const created = await watchlistApi.create(name, seed)
      await refresh(created.id)
      showToast.success(`Imported ${seed.length} instruments into ${name}`)
    } catch (error) {
      showToast.error(
        error instanceof SyntaxError
          ? 'That file is not valid JSON'
          : watchlistError(error, (error as Error)?.message || 'Could not import that file')
      )
    }
  }

  /**
   * The list as it renders: section headers interleaved with rows.
   *
   * The stored order is already grouped — rows keep position within their
   * section — so grouping is a fold that emits a header whenever the section
   * name changes. With no section named anywhere the header would only say
   * "everything", which the column header already says, so it is skipped.
   */
  const renderSections = useCallback(
    (rows: WatchlistItem[]): Array<
      | { kind: 'section'; name: string | null; dim: boolean; count: number }
      | { kind: 'row'; item: WatchlistItem; index: number }
    > => {
      if (!display.sections) return rows.map((item, index) => ({ kind: 'row' as const, item, index }))
      const out: Array<
        | { kind: 'section'; name: string | null; dim: boolean; count: number }
        | { kind: 'row'; item: WatchlistItem; index: number }
      > = []
      let current: string | null | undefined = undefined
      for (let i = 0; i < rows.length; i++) {
        const name = itemSection(rows[i])
        if (name !== current) {
          current = name
          let count = 0
          for (let j = i; j < rows.length && itemSection(rows[j]) === name; j++) count++
          out.push({ kind: 'section', name, dim: false, count })
        }
        out.push({ kind: 'row', item: rows[i], index: i })
      }
      return out
    },
    [display.sections]
  )

  /** A row as it renders — extracted so section headers can interleave. */
  const itemRow = (item: WatchlistItem, _index: number) => {
            const key = `${item.exchange}:${item.symbol}`
            const quote = quotes[key]
            // Three states, not two: no previous close means no direction.
            const direction =
              quote?.changePercent == null ? 'flat' : quote.changePercent >= 0 ? 'up' : 'down'
            // Which side of the hovered row the dragged one lands on,
            // decided by the pointer's half and recorded in onDragOver.
            const dropBelow = dropAfterId
            return (
              <div
                key={item.id}
                draggable
                onDragStart={(e) => {
                  // Firefox refuses to begin a drag unless dataTransfer carries
                  // something, so this is what makes reordering work there.
                  e.dataTransfer.effectAllowed = 'move'
                  e.dataTransfer.setData('text/plain', String(item.id))
                  setDragId(item.id)
                }}
                onDragOver={(e) => {
                  e.preventDefault()
                  e.dataTransfer.dropEffect = 'move'
                  setOverId(item.id)
                  // Above or below the midline decides which side the row
                  // lands on — the line the user sees is the line they get.
                  const rect = e.currentTarget.getBoundingClientRect()
                  setDropAfterId(e.clientY > rect.top + rect.height / 2)
                  // Back over rows after a pass over a header: the header's
                  // highlight must not stick.
                  setOverSection((s) => (s === undefined ? s : undefined))
                }}
                onDragLeave={() => setOverId((id) => (id === item.id ? null : id))}
                onDrop={() => void dropOn(item.id)}
                onDragEnd={() => {
                  setDragId(null)
                  setOverId(null)
                }}
                style={{ gridTemplateColumns: gridTemplate }}
                className={cn(
                  'grid gap-x-1.5',
                  'group relative items-center px-2 py-1 text-[12px] transition-colors hover:bg-accent/50',
                  // The charted row carries a left marker as well as a wash.
                  // Hover alone was the same bg-accent, so pointing at any row
                  // made it look like the one currently on the chart.
                  //
                  // var(--color-primary), never hsl(var(--primary)): this app
                  // carries two token systems and the later, unlayered one
                  // defines --primary as a complete oklch(), so hsl() of it is
                  // invalid and the whole box-shadow is dropped. Tailwind's
                  // @theme maps --color-primary to whichever is live. See the
                  // long-form account in TickBox.tsx.
                  //
                  // hover:bg-accent/50 outranks bg-accent on specificity, so the
                  // wash needs !important or hovering the charted row lightens
                  // it into looking like every other hovered row.
                  activeSymbol === key &&
                    '!bg-accent font-medium shadow-[inset_2px_0_0_0_var(--color-primary)]',
                  dragId === item.id && 'opacity-40',
                  // A line where the row will land. An inset shadow rather than
                  // a border: a border adds 2px to an auto-height row, so every
                  // row below it jumped as the pointer moved down the list.
                  overId === item.id &&
                    dragId !== item.id &&
                    (dropBelow
                      ? 'shadow-[inset_0_-2px_0_0_var(--color-primary)]'
                      : 'shadow-[inset_0_2px_0_0_var(--color-primary)]')
                )}
              >
                {/* A real button stretched over the row rather than a
                    role="button" div. The row carries a delete control of its
                    own, and a button inside a button is invalid HTML; laying
                    the click target underneath keeps both real buttons and
                    gives keyboard users the row for free. The drag handlers
                    stay on the wrapper, which still receives them because a
                    plain button is not itself draggable. */}
                <button
                  type="button"
                  onClick={() => {
                    // GLOBAL rows are chartable too: the terminal feeds the
                    // pane from the Yahoo-backed global history plugin, so a
                    // click loads the international benchmark's dollar chart.
                    setSyncSymbol(`${item.exchange}:${item.symbol}`)
                    onPick({ symbol: item.symbol, exchange: item.exchange })
                  }}
                  onKeyDown={(e) => {
                    // Removing from the row itself is what lets the trash stay
                    // out of the tab order: two stops per row would be sixty
                    // on a thirty-symbol list before anything else is reachable.
                    if (e.key === 'Delete') {
                      e.preventDefault()
                      void removeSymbol(item)
                    }
                  }}
                  className={cn(
                    'absolute inset-0 rounded-sm cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring'
                  )}
                  aria-label={`Chart ${item.symbol} on ${item.exchange}`}
                  // The charted row said so only in colour. This states it, so
                  // a screen reader hears which instrument is on the chart and a
                  // test can assert the fact rather than a Tailwind class that
                  // jsdom has no cascade to evaluate.
                  aria-current={activeSymbol === key ? true : undefined}
                />

                {/* Visible drag handle for reordering rows and moving across sections */}
                <span
                  className="relative z-10 -ml-1 mr-0.5 flex h-4 w-4 shrink-0 cursor-grab items-center justify-center text-muted-foreground/30 transition-colors hover:text-foreground group-hover:text-muted-foreground/70 active:cursor-grabbing"
                  title="Drag to rearrange"
                >
                  <GripVertical className="h-3.5 w-3.5" />
                </span>

                <span className="pointer-events-none relative flex min-w-0 items-center gap-1.5">
                  {display.logo && (
                    <span
                      className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold text-white"
                      style={{ backgroundColor: symbolTint(item.symbol) }}
                      aria-hidden="true"
                    >
                      {item.symbol.charAt(0)}
                    </span>
                  )}
                  <span className="truncate font-medium" title={item.symbol}>
                    {item.symbol}
                  </span>
                  {display.exchange && (
                    <span className="shrink-0 text-[10px] text-muted-foreground">
                      {item.exchange}
                    </span>
                  )}
                </span>

                {/* Always visible. Hiding the number the user hovered in order
                    to read it, so a control can borrow its cell, trades the
                    panel's whole purpose for one action used once per row. */}
                {shownColumns.map((column) => (
                  <span
                    key={column.id}
                    className={cn(
                      'pointer-events-none relative text-right tabular-nums',
                      // Only a column carrying direction is coloured.
                      column.tone && direction === 'up' && 'text-emerald-600 dark:text-emerald-400',
                      column.tone && direction === 'down' && 'text-rose-600 dark:text-rose-400',
                      (!column.tone || direction === 'flat') && 'text-muted-foreground',
                      !column.tone && quote && 'text-foreground'
                    )}
                  >
                    {quote ? column.get(quote) : '-'}
                  </span>
                ))}

                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      type="button"
                      tabIndex={-1}
                      className="relative z-10 flex h-4 w-4 items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100 data-[state=open]:opacity-100"
                      title={`Actions for ${item.symbol}`}
                      aria-label={`Actions for ${item.symbol}`}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <MoreHorizontal className="h-3.5 w-3.5" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-48" onClick={(e) => e.stopPropagation()}>
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger>
                        <FolderPlus className="mr-2 h-3.5 w-3.5" />
                        Move to section
                      </DropdownMenuSubTrigger>
                      <DropdownMenuSubContent className="w-44">
                        {item.section?.trim() ? (
                          <DropdownMenuItem onClick={() => void moveItemToSection(item, null)}>
                            (no section)
                          </DropdownMenuItem>
                        ) : null}
                        {sectionNames
                          .filter((n) => n !== item.section?.trim())
                          .map((name) => (
                            <DropdownMenuItem key={name} onClick={() => void moveItemToSection(item, name)}>
                              {name}
                            </DropdownMenuItem>
                          ))}
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          onClick={() => {
                            setSectionName('')
                            setSectionDialog({ item })
                          }}
                        >
                          <Plus className="mr-2 h-3.5 w-3.5" />
                          New section…
                        </DropdownMenuItem>
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      variant="destructive"
                      onClick={() => void removeSymbol(item)}
                    >
                      <Trash2 className="mr-2 h-3.5 w-3.5" />
                      Remove {item.symbol}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  const nameDialogTitle =
    nameDialog?.mode === 'rename'
      ? 'Rename list'
      : nameDialog?.mode === 'copy'
        ? 'Copy list'
        : 'New list'

  return (
    <PanelShell
      id="oa-panel-watchlist"
      label="Watchlist"
      storageKey="oa-trading-watchlist-width"
      minWidth={minWidth}
    >
      {/* Header: which list, and what can be done to it. Its height lands the
          rule on the same line as every pane toolbar's. */}
      <div className={PANEL_HEADER}>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex h-8 min-w-0 flex-1 items-center gap-1 rounded-md px-1.5 text-left text-[13px] font-medium transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              disabled={!active}
            >
              <span className="truncate">{active?.name ?? 'Watchlist'}</span>
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            <DropdownMenuItem onSelect={() => setNameDialog({ mode: 'create', value: '' })}>
              Create new list...
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!active}
              onSelect={() =>
                active && setNameDialog({ mode: 'copy', value: `${active.name} copy` })
              }
            >
              Make a copy...
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!active}
              onSelect={() => active && setNameDialog({ mode: 'rename', value: active.name })}
            >
              Rename...
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => fileRef.current?.click()}>
              Import list...
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!active || items.length === 0} onSelect={exportList}>
              Export list...
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              disabled={!active || items.length === 0}
              onSelect={() => active && setConfirm({ kind: 'clear', name: active.name })}
            >
              Clear list
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!active}
              className="text-destructive focus:text-destructive"
              onSelect={() => active && setConfirm({ kind: 'delete', name: active.name })}
            >
              Delete list
            </DropdownMenuItem>

            {lists.length > 1 && (
              <>
                <DropdownMenuSeparator />
                <div className="px-2 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
                  Your lists
                </div>
                {lists.map((list) => (
                  <DropdownMenuItem
                    key={list.id}
                    onSelect={() => setActiveId(list.id)}
                    className={cn(list.id === active?.id && 'bg-accent')}
                  >
                    <span className="flex-1 truncate">{list.name}</span>
                    <span className="ml-2 shrink-0 text-[11px] tabular-nums text-muted-foreground">
                      {list.items.length}
                    </span>
                  </DropdownMenuItem>
                ))}
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          disabled={!active}
          onClick={() => setSearchOpen(true)}
          title="Add instrument"
          aria-label="Add instrument"
        >
          <Plus className="h-4 w-4" />
        </Button>

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          disabled={!active}
          onClick={() => {
            setSectionName('')
            setSectionDialog({ item: null })
          }}
          title="Add section"
          aria-label="Add section"
        >
          <FolderPlus className="h-4 w-4" />
        </Button>

        {/* What the rows show. Separate from the list menu beside it: that
            one acts on the list, this one only changes how it is drawn. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0"
              title="Customise columns"
              aria-label="Customise columns"
            >
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            <DropdownMenuLabel className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
              Columns
            </DropdownMenuLabel>
            {COLUMNS.map((column) => (
              <DropdownMenuCheckboxItem
                key={column.id}
                checked={display.columns.includes(column.id)}
                // The menu stays open: choosing columns is several decisions,
                // and closing after each one would mean reopening every time.
                onSelect={(e) => e.preventDefault()}
                onCheckedChange={() => toggleColumn(column.id)}
                className="text-[12px]"
              >
                {column.label === 'Chg'
                  ? 'Change'
                  : column.label === 'Chg%'
                    ? 'Change %'
                    : column.label === 'Vol'
                      ? 'Volume'
                      : column.label}
              </DropdownMenuCheckboxItem>
            ))}

            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
              Symbol display
            </DropdownMenuLabel>
            <DropdownMenuCheckboxItem
              checked={display.logo}
              onSelect={(e) => e.preventDefault()}
              onCheckedChange={(v) => setDisplay((prev) => ({ ...prev, logo: v }))}
              className="text-[12px]"
            >
              Symbol letter
            </DropdownMenuCheckboxItem>
            <DropdownMenuCheckboxItem
              checked={display.exchange}
              onSelect={(e) => e.preventDefault()}
              onCheckedChange={(v) => setDisplay((prev) => ({ ...prev, exchange: v }))}
              className="text-[12px]"
            >
              Exchange
            </DropdownMenuCheckboxItem>
            <DropdownMenuCheckboxItem
              checked={display.sections}
              onSelect={(e) => e.preventDefault()}
              onCheckedChange={(v) => setDisplay((prev) => ({ ...prev, sections: v }))}
              className="text-[12px]"
            >
              Sections
            </DropdownMenuCheckboxItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Column header, built from the same selection as the rows below */}
      <div
        className="grid shrink-0 gap-x-1.5 border-b px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70"
        style={{ gridTemplateColumns: gridTemplate }}
      >
        <span>Symbol</span>
        {shownColumns.map((column) => (
          <span key={column.id} className="text-right">
            {column.label}
          </span>
        ))}
        <span />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <p className="p-3 text-[12px] text-muted-foreground">Loading...</p>
        ) : loadError ? (
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <p className="text-[12px] text-muted-foreground">{loadError}</p>
            <Button
              variant="outline"
              size="sm"
              className="h-7 gap-1.5"
              onClick={() => void retry()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Retry
            </Button>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <Search className="h-5 w-5 text-muted-foreground/50" />
            <p className="text-[12px] text-muted-foreground">This list is empty.</p>
            <Button variant="outline" size="sm" className="h-7" onClick={() => setSearchOpen(true)}>
              Add an instrument
            </Button>
          </div>
        ) : (
          renderSections(items).map((row) =>
            row.kind === 'section' ? (
              // A header is a drop target too: dropping a row on it files the
              // row under that section, which beats hunting for the boundary
              // between two groups.
              <div
                key={`sec:${row.name ?? ''}`}
                onDragOver={(e) => {
                  e.preventDefault()
                  e.dataTransfer.dropEffect = 'move'
                  setOverSection(row.name)
                }}
                onDragLeave={() => setOverSection((s) => (s === row.name ? undefined : s))}
                onDrop={(e) => {
                  e.preventDefault()
                  void dropOnSection(row.name)
                }}
                className={cn(
                  'sticky top-0 z-10 flex items-center gap-1.5 border-b bg-background/95 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur',
                  row.dim && 'opacity-70',
                  overSection === row.name &&
                    dragId != null &&
                    'bg-accent text-foreground shadow-[inset_0_2px_0_0_var(--color-primary)]'
                )}
              >
                <span className="truncate">{row.dim ? 'Symbols' : row.name}</span>
                <span className="ml-auto text-[10px] font-medium tabular-nums text-muted-foreground/70">
                  {row.count}
                </span>
              </div>
            ) : (
              itemRow(row.item, row.index)
            )
          )
        )}
      </div>

      {/* Hidden file input backing "Import list..." */}
      <input
        ref={fileRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) void importList(file)
          // Reset so choosing the same file twice fires change both times.
          e.target.value = ''
        }}
      />

      {/* Where these numbers came from, whenever that is not the live feed.
          Matches the option chain's freshness line. */}
      {feedNote && (
        <p
          className={cn(
            'shrink-0 border-t px-2 py-1 text-[10px]',
            stale ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
          )}
        >
          {feedNote}
        </p>
      )}

      <SymbolSearchDialog
        open={searchOpen}
        onOpenChange={setSearchOpen}
        search={search}
        onPick={(row) => void addSymbol(row)}
      />

      {/* Create / rename / copy */}
      <Dialog open={nameDialog !== null} onOpenChange={(open) => !open && setNameDialog(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{nameDialogTitle}</DialogTitle>
            <DialogDescription>
              {nameDialog?.mode === 'copy'
                ? 'The new list starts with the same instruments.'
                : 'Names must be unique.'}
            </DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={nameDialog?.value ?? ''}
            maxLength={64}
            placeholder="List name"
            onChange={(e) =>
              setNameDialog((prev) => (prev ? { ...prev, value: e.target.value } : prev))
            }
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submitName()
            }}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setNameDialog(null)}>
              Cancel
            </Button>
            <Button disabled={!nameDialog?.value.trim()} onClick={() => void submitName()}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* New section: names a group and files one row into it */}
      <Dialog open={sectionDialog !== null} onOpenChange={(open) => !open && setSectionDialog(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>New section</DialogTitle>
            <DialogDescription>
              {sectionDialog?.item
                ? `Group for ${sectionDialog.item.symbol} — it moves under this name.`
                : 'Create a named section header to organize symbols in your watchlist.'}
            </DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={sectionName}
            maxLength={32}
            placeholder="Section name"
            onChange={(e) => setSectionName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submitNewSection()
            }}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setSectionDialog(null)}>
              Cancel
            </Button>
            <Button disabled={!sectionName.trim()} onClick={() => void submitNewSection()}>
              {sectionDialog?.item ? 'Create & move' : 'Create section'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Clear / delete */}
      <Dialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{confirm?.kind === 'delete' ? 'Delete list' : 'Clear list'}</DialogTitle>
            <DialogDescription>
              {confirm?.kind === 'delete'
                ? `"${confirm?.name}" and everything in it will be removed. This cannot be undone.`
                : `Every instrument will be removed from "${confirm?.name}". The list itself stays.`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void runConfirm()}>
              {confirm?.kind === 'delete' ? 'Delete' : 'Clear'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PanelShell>
  )
}
