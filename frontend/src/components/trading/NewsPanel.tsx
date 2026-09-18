import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, ExternalLink, Loader2, RefreshCw } from 'lucide-react'
import { newsApi, type NewsItem } from '@/api/market-brief-news'
import { PanelShell } from './panelShell'
import { cn } from '@/lib/utils'

/**
 * News side panel with a TradingView tab.
 *
 * Tabs:
 *  - "TV + Feeds": TradingView news-headlines feed for the focused chart's
 *    symbol (indices, MCX commodities, equities) merged with keyword-matched
 *    Indian/global RSS items — this is the TradingView news source integration.
 *  - "Markets": the merged RSS feed (ET, Mint, NDTV Profit, Moneycontrol,
 *    Yahoo, CNBC), newest first.
 */

const REFRESH_MS = 60_000 // headline freshness

function timeAgo(ts?: number): string {
  if (!ts) return ''
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (secs < 90) return 'now'
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`
  return `${Math.floor(secs / 86400)}d`
}

function sentimentDot(s?: string): string {
  if (s === 'Bullish') return 'bg-emerald-500'
  if (s === 'Bearish') return 'bg-rose-500'
  return 'bg-amber-500/60'
}

function NewsRow({ n, expanded, onToggle }: { n: NewsItem; expanded: boolean; onToggle: () => void }) {
  return (
    <div className="border-b border-border/40 hover:bg-accent/40">
      <button
        type="button"
        onClick={onToggle}
        className="block w-full px-2 py-1.5 text-left"
        title={expanded ? 'Collapse' : 'Expand summary'}
      >
        <div className="flex items-start justify-between gap-1.5">
          <span className="text-[11px] leading-snug text-foreground">{n.title}</span>
          {expanded ? (
            <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-[9px] text-muted-foreground">
          {n.sentiment && <span className={cn('h-1.5 w-1.5 rounded-full', sentimentDot(n.sentiment))} title={n.sentiment} />}
          <span className="font-medium">{n.source}</span>
          <span>·</span>
          <span className="tabular-nums">{timeAgo(n.published)}</span>
          {!expanded && n.summary && <span className="min-w-0 truncate">· {n.summary}</span>}
        </div>
      </button>
      {expanded && (
        <div className="border-t border-border/40 bg-muted/20 px-2 py-1.5">
          {n.summary ? (
            <p className="text-[10px] leading-relaxed text-foreground/90">{n.summary}</p>
          ) : (
            <p className="text-[10px] italic text-muted-foreground">No summary available for this story.</p>
          )}
          {n.link && (
            <a
              href={n.link}
              target="_blank"
              rel="noreferrer"
              className="mt-1 inline-flex items-center gap-1 text-[10px] font-medium text-primary hover:underline"
            >
              Read full article <ExternalLink className="h-2.5 w-2.5" />
            </a>
          )}
        </div>
      )}
    </div>
  )
}

function ItemList({ items, empty }: { items: NewsItem[]; empty: string }) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  if (!items.length) {
    return <div className="p-2 text-[11px] text-muted-foreground">{empty}</div>
  }
  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  return (
    <>
      {items.map((n, i) => {
        const id = n.id ?? `${i}-${n.link}`
        return (
          <NewsRow key={id} n={n} expanded={expandedIds.has(id)} onToggle={() => toggle(id)} />
        )
      })}
    </>
  )
}

export function NewsPanel({ activeSymbol }: { apiKey: string; activeSymbol: string | null }) {
  const [tab, setTab] = useState<'tv' | 'markets'>('tv')
  const [symbol, setSymbol] = useState<string>(activeSymbol ?? 'NSE:NIFTY')
  const [symbolInput, setSymbolInput] = useState<string>(activeSymbol ?? 'NSE:NIFTY')
  const [tvItems, setTvItems] = useState<NewsItem[]>([])
  const [tvSources, setTvSources] = useState<Array<{ name: string; count: number }>>([])
  const [sourceFilter, setSourceFilter] = useState<string>('All')
  const [rssItems, setRssItems] = useState<NewsItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Adopt the focused chart's symbol when the pane changes
  useEffect(() => {
    if (activeSymbol) {
      setSymbol(activeSymbol)
      setSymbolInput(activeSymbol)
    }
  }, [activeSymbol])

  const load = async () => {
    try {
      setError(null)
      if (tab === 'tv') {
        const res = await newsApi.getSymbolNews(symbol, 40)
        setTvItems(res.items ?? [])
        setTvSources(res.sources ?? [])
      } else {
        const res = await newsApi.getNews(60)
        setRssItems(res.articles ?? [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load news')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, symbol])

  const filtered = tab === 'tv'
    ? (sourceFilter === 'All' ? tvItems : tvItems.filter((n) => n.source === sourceFilter))
    : rssItems

  return (
    <PanelShell id="oa-panel-news" label="News" storageKey="oa-trading-news-width" defaultWidth={340}>
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="text-xs font-semibold text-foreground">News</div>
        <button type="button" onClick={load} className="rounded p-1 hover:bg-accent" title="Refresh">
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
        </button>
      </div>

      <div className="flex border-b border-border text-[11px]">
        {([['tv', 'TV + Feeds'], ['markets', 'Markets']] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn('flex-1 px-2 py-1', tab === id ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground')}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'tv' && (
        <div className="border-b border-border px-2 py-1">
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              setSymbol(symbolInput.trim().toUpperCase() || 'NSE:NIFTY')
            }}
          >
            <input
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value)}
              placeholder="NSE:NIFTY / MCX:CRUDEOIL"
              className="min-w-0 flex-1 rounded border border-border bg-transparent px-1 py-0.5 text-[10px]"
            />
            <button type="submit" className="rounded border border-border px-1.5 py-0.5 text-[10px] hover:bg-accent">
              Go
            </button>
          </form>
          {tvSources.length > 1 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {tvSources.map((s) => (
                <button
                  key={s.name}
                  type="button"
                  onClick={() => setSourceFilter(s.name)}
                  className={cn('rounded border px-1 py-px text-[9px]', sourceFilter === s.name ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:text-foreground')}
                >
                  {s.name} ({s.count})
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {error && <div className="m-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>}
        <ItemList
          items={filtered}
          empty={tab === 'tv' ? `No TradingView headlines for ${symbol}.` : 'No market headlines right now.'}
        />
      </div>

      <div className="border-t border-border px-2 py-1 text-[9px] text-muted-foreground">
        TradingView headlines + Indian & global RSS feeds
      </div>
    </PanelShell>
  )
}
