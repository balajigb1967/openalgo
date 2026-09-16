import { useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { calendarApi, type EconomicEvent, type Holiday } from '@/api/market-brief-news'
import { PanelShell } from './panelShell'
import { cn } from '@/lib/utils'

/**
 * Market calendar side panel.
 *
 * Tabs:
 *  - "Economic": this week's macro events (TradingView-week feed), next-up
 *    first, with impact badges and actual/forecast/previous numbers.
 *  - "Holidays": NSE / BSE / MCX holiday lists plus a today-is-a-trading-day
 *    status line. All data is public and broker-independent.
 */

const REFRESH_MS = 600_000

function impactChip(impact: string): string {
  if (impact === 'High') return 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30'
  if (impact === 'Medium') return 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30'
  return 'bg-muted text-muted-foreground border-border'
}

function eventClock(iso: string): string {
  // faireconomy dates are UTC ISO like 2026-09-16T08:30:00Z — show IST.
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(11, 16)
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })
}

function eventDay(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const today = new Date()
  const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate()
  const wd = d.toLocaleDateString('en-IN', { weekday: 'short' })
  return sameDay ? 'Today' : wd
}

function EventRow({ e }: { e: EconomicEvent }) {
  const isHigh = e.impact === 'High'
  return (
    <div className={cn('border-b border-border/40 px-2 py-1.5', isHigh && 'bg-rose-500/[0.03]')}>
      <div className="flex items-start justify-between gap-1.5">
        <span className="text-[11px] leading-snug text-foreground">{e.title}</span>
        <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] font-semibold', impactChip(e.impact))}>
          {e.impact}
        </span>
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[9px] text-muted-foreground tabular-nums">
        <span className="font-medium text-foreground/70">{e.currency || '—'}</span>
        <span>·</span>
        <span>{eventDay(e.date)} {eventClock(e.date)} IST</span>
        {e.forecast && <span className="min-w-0 truncate">· fc {e.forecast}</span>}
        {e.actual && <span className="font-semibold text-foreground">· act {e.actual}</span>}
      </div>
    </div>
  )
}

function HolidayList({ items, highlight }: { items: Holiday[]; highlight?: boolean }) {
  const today = new Date().toISOString().slice(0, 10)
  const upcoming = items.filter((h) => h.date >= today)
  if (!upcoming.length) {
    return <div className="p-2 text-[11px] text-muted-foreground">No upcoming holidays on record.</div>
  }
  return (
    <>
      {upcoming.map((h) => (
        <div key={`${h.date}-${h.name}`} className={cn('flex items-center justify-between border-b border-border/40 px-2 py-1.5', highlight && 'bg-primary/[0.03]')}>
          <div className="min-w-0">
            <div className="text-[11px] leading-snug text-foreground">{h.name}</div>
            <div className="text-[9px] text-muted-foreground">{h.day}</div>
          </div>
          <span className="shrink-0 text-[10px] font-medium tabular-nums text-muted-foreground">{h.date_display}</span>
        </div>
      ))}
    </>
  )
}

export function CalendarPanel(_props: { apiKey: string }) {
  const [tab, setTab] = useState<'economic' | 'holidays'>('economic')
  const [econ, setEcon] = useState<Awaited<ReturnType<typeof calendarApi.getEconomic>> | null>(null)
  const [hol, setHol] = useState<Awaited<ReturnType<typeof calendarApi.getHolidays>> | null>(null)
  const [holExchange, setHolExchange] = useState<'nse' | 'bse' | 'mcx'>('nse')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async (refresh = false) => {
    try {
      setError(null)
      if (tab === 'economic') {
        setEcon(await calendarApi.getEconomic(refresh))
      } else {
        setHol(await calendarApi.getHolidays(refresh))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load calendar')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    load()
    const t = setInterval(() => load(), REFRESH_MS)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  return (
    <PanelShell id="oa-panel-calendar" label="Calendar" storageKey="oa-trading-calendar-width" defaultWidth={340}>
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="text-xs font-semibold text-foreground">Market Calendar</div>
        <button type="button" onClick={() => load(true)} className="rounded p-1 hover:bg-accent" title="Force refresh">
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
        </button>
      </div>

      <div className="flex border-b border-border text-[11px]">
        {([['economic', 'Economic'], ['holidays', 'Holidays']] as const).map(([id, label]) => (
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

      <div className="flex-1 overflow-y-auto">
        {error && <div className="m-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>}

        {tab === 'economic' && (
          econ?.upcoming?.length ? (
            <>
              <div className="px-2 py-1 text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">
                Upcoming this week ({econ.upcoming.length})
              </div>
              {econ.upcoming.map((e, i) => <EventRow key={`${e.title}-${i}`} e={e} />)}
              {econ.recent?.length > 0 && (
                <>
                  <div className="px-2 py-1 text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">Recent</div>
                  {econ.recent.slice(0, 6).map((e, i) => <EventRow key={`r-${e.title}-${i}`} e={e} />)}
                </>
              )}
            </>
          ) : (
            <div className="p-2 text-[11px] text-muted-foreground">
              {econ ? 'No macro events on the calendar this week.' : 'Loading…'}
            </div>
          )
        )}

        {tab === 'holidays' && (
          hol ? (
            <>
              <div className="border-b border-border/40 bg-muted/20 px-2 py-1.5 text-[10px]">
                <span className={cn('font-semibold', hol.today_status.nse_trading_day ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400')}>
                  {hol.today_status.nse_trading_day ? 'NSE/BSE: trading today' : 'NSE/BSE: holiday today'}
                </span>
                <span className="mx-1.5 text-border">|</span>
                <span className={cn('font-semibold', hol.today_status.mcx_trading_day ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400')}>
                  {hol.today_status.mcx_trading_day ? 'MCX: trading today' : 'MCX: holiday today'}
                </span>
                {hol.next_nse_holiday && (
                  <div className="mt-0.5 text-[9px] text-muted-foreground">
                    Next holiday: {hol.next_nse_holiday.name} · {hol.next_nse_holiday.date_display}
                  </div>
                )}
              </div>
              <div className="flex border-b border-border/40 text-[10px]">
                {(['nse', 'bse', 'mcx'] as const).map((x) => (
                  <button
                    key={x}
                    type="button"
                    onClick={() => setHolExchange(x)}
                    className={cn('flex-1 px-2 py-1 uppercase', holExchange === x ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground')}
                  >
                    {x}
                  </button>
                ))}
              </div>
              <HolidayList items={hol[holExchange]} />
            </>
          ) : (
            <div className="p-2 text-[11px] text-muted-foreground">Loading…</div>
          )
        )}
      </div>

      <div className="border-t border-border px-2 py-1 text-[9px] text-muted-foreground">
        TradingView-week macro feed · NSE/BSE/MCX holiday schedules
      </div>
    </PanelShell>
  )
}
