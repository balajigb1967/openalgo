import { useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { briefApi, type BriefQuote, type MarketBriefResponse } from '@/api/market-brief-news'
import { PanelShell } from './panelShell'
import { cn } from '@/lib/utils'

/**
 * Market Brief side panel.
 *
 * Ported from fno-trader-pro's Market Brief widget: overnight cues, live
 * session stance, indices/commodities tape, NIFTY option internals, tactical
 * gameplan with pivots, geopolitical drivers and economic events. The backend
 * (services/market_brief_service.py) assembles the snapshot from TradingView
 * scanner quotes, NSE public APIs and OpenAlgo's own option-chain service;
 * this panel renders it in sidebar-sized sections.
 */

const REFRESH_MS = 60_000

function chpColor(v?: number | null): string {
  if (v === null || v === undefined) return 'text-muted-foreground'
  if (v > 0) return 'text-emerald-600 dark:text-emerald-400'
  if (v < 0) return 'text-rose-600 dark:text-rose-400'
  return 'text-muted-foreground'
}

function fmtPct(v?: number | null): string {
  if (v === null || v === undefined) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function fmtLtp(v?: number | null): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('en-IN', { maximumFractionDigits: 2 })
}

function QuoteRow({ q }: { q: BriefQuote }) {
  return (
    <div className="flex items-center justify-between px-1 py-0.5 text-[11px] tabular-nums">
      <span className="text-foreground">{q.label}</span>
      <span className="flex items-center gap-1.5">
        <span>{fmtLtp(q.ltp)}</span>
        <span className={cn('w-14 text-right', chpColor(q.chp))}>{fmtPct(q.chp)}</span>
      </span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-border/60 px-2 py-1.5">
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</div>
      {children}
    </div>
  )
}

export function MarketBriefPanel(_props: { apiKey: string }) {
  const [data, setData] = useState<MarketBriefResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async (refresh = false) => {
    try {
      setError(null)
      setData(await briefApi.getBrief(refresh))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load brief')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(() => load(), REFRESH_MS)
    return () => clearInterval(t)
  }, [])

  const stance = data?.session_stance
  const cues = data?.overnight_cues
  const gameplan = data?.gameplan
  const geo = data?.geopolitical
  const niftyPlan = gameplan?.nifty
  const bnPlan = gameplan?.banknifty

  return (
    <PanelShell id="oa-panel-brief" label="Market Brief" storageKey="oa-trading-brief-width" defaultWidth={340}>
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="text-xs font-semibold text-foreground">Market Brief</div>
        <div className="flex items-center gap-1">
          {data && <span className="text-[9px] text-muted-foreground tabular-nums">{data.generated_at?.slice(11, 16)}</span>}
          <button type="button" onClick={() => load(true)} className="rounded p-1 hover:bg-accent" title="Force refresh">
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {error && <div className="m-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>}
        {!data && !error && (
          <div className="flex items-center justify-center p-4 text-[11px] text-muted-foreground">
            <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> Building brief…
          </div>
        )}

        {data && (
          <>
            {/* Live session stance */}
            <div className="border-b border-border/60 px-2 py-1.5">
              <div className="flex items-center justify-between">
                <span className={cn('text-[11px] font-bold',
                  stance?.stance === 'BULLISH' ? 'text-emerald-600 dark:text-emerald-400' : stance?.stance === 'BEARISH' ? 'text-rose-600 dark:text-rose-400' : 'text-amber-600 dark:text-amber-400')}>
                  {stance?.icon} {stance?.stance}
                </span>
                {stance?.is_live && (
                  <span className="flex items-center gap-1 text-[9px] text-emerald-600 dark:text-emerald-400">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" /> LIVE
                  </span>
                )}
              </div>
              <div className="text-[9px] text-muted-foreground">{stance?.phase_label} · upd {stance?.updated_at}</div>
              <p className="mt-1 text-[10px] leading-snug text-foreground">{stance?.narrative}</p>
            </div>

            <Section title="Overnight Cues">
              <div className="flex items-center justify-between text-[10px]">
                <span className="text-muted-foreground">Sentiment</span>
                <span className={cn('font-semibold', cues?.sentiment?.startsWith('BULLISH') ? 'text-emerald-600 dark:text-emerald-400' : cues?.sentiment?.startsWith('BEARISH') ? 'text-rose-600 dark:text-rose-400' : 'text-amber-600 dark:text-amber-400')}>
                  {cues?.sentiment}
                </span>
              </div>
              {cues?.summary && (
                <p className="mt-1 text-[9px] leading-snug text-muted-foreground">{cues.summary}</p>
              )}
              <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums">
                <span className="text-muted-foreground">FII / DII ({cues?.institutional_flow?.date?.slice(0, 6) ?? '—'})</span>
                <span className="flex gap-2">
                  <span className={cn((cues?.institutional_flow?.fii_net ?? '').startsWith('+') ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>{cues?.institutional_flow?.fii_net ?? '—'}</span>
                  <span className={cn((cues?.institutional_flow?.dii_net ?? '').startsWith('+') ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>{cues?.institutional_flow?.dii_net ?? '—'}</span>
                </span>
              </div>
              <div className="mt-0.5 text-[9px] text-muted-foreground">{cues?.institutional_flow?.net_bias}</div>
            </Section>

            {(cues?.us_close?.length ?? 0) > 0 && (
              <Section title="US Close (Overnight)">
                {cues!.us_close.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}
            {(cues?.asia_morning?.length ?? 0) > 0 && (
              <Section title="Asia Morning">
                {cues!.asia_morning.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}
            {(cues?.europe_session?.length ?? 0) > 0 && (
              <Section title="Europe">
                {cues!.europe_session.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}
            {(cues?.gift_nifty?.length ?? 0) > 0 && (
              <Section title="GIFT Nifty">
                {cues!.gift_nifty.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}
            {(cues?.indian_adrs?.length ?? 0) > 0 && (
              <Section title="Indian ADRs">
                {cues!.indian_adrs.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}
            {(cues?.macro_indicators?.length ?? 0) > 0 && (
              <Section title="Macro (10Y / DXY / USDINR)">
                {cues!.macro_indicators.map((q) => <QuoteRow key={q.label} q={q} />)}
              </Section>
            )}

            <Section title="Indices">
              {data.indices.map((q) => <QuoteRow key={q.label} q={q} />)}
            </Section>

            <Section title="Commodities (MCX)">
              {data.commodities.map((q) => <QuoteRow key={q.label} q={q} />)}
            </Section>

            <Section title="NIFTY Options">
              <div className="grid grid-cols-3 gap-1 text-[10px] tabular-nums">
                <div><div className="text-muted-foreground">PCR</div><div className="font-semibold">{data.options?.pcr ?? '—'}</div></div>
                <div><div className="text-muted-foreground">Max Pain</div><div className="font-semibold">{fmtLtp(data.options?.max_pain)}</div></div>
                <div><div className="text-muted-foreground">ATM IV</div><div className="font-semibold">{data.options?.atm_iv ?? '—'}</div></div>
              </div>
              <div className="mt-1 text-[10px]">
                <span className="text-muted-foreground">Likely: </span>
                <span className={cn('font-semibold', data.options?.likely_direction === 'BULLISH' ? 'text-emerald-600 dark:text-emerald-400' : data.options?.likely_direction === 'BEARISH' ? 'text-rose-600 dark:text-rose-400' : 'text-amber-600 dark:text-amber-400')}>
                  {data.options?.likely_direction ?? '—'}
                </span>
                <span className="text-muted-foreground"> ({data.options?.likely_confidence ?? '--'}%) · range {data.options?.likely_range ?? '—'}</span>
              </div>
            </Section>

            {niftyPlan && bnPlan && (
              <Section title="Gameplan">
                {[niftyPlan, bnPlan].map((p, i) => (
                  <div key={i} className="mb-1.5 rounded border border-border/60 px-1.5 py-1">
                    <div className="flex items-center justify-between text-[10px]">
                      <span className="font-semibold">{p.name}</span>
                      <span className={cn(p.chp >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>{fmtLtp(p.ltp)} ({fmtPct(p.chp)})</span>
                    </div>
                    <div className="mt-0.5 flex justify-between text-[9px] tabular-nums text-muted-foreground">
                      <span className="text-rose-600 dark:text-rose-400">S1 {p.s1}</span>
                      <span>P {p.pivot}</span>
                      <span className="text-emerald-600 dark:text-emerald-400">R1 {p.r1}</span>
                    </div>
                    <div className="mt-0.5 text-[9px] leading-snug text-foreground">{p.action}</div>
                    <div className="text-[9px] font-medium text-primary">{p.bias}</div>
                  </div>
                ))}
              </Section>
            )}

            {geo?.drivers && (
              <Section title="Geo & Macro">
                {geo.drivers.map((d, i) => (
                  <div key={i} className="mb-1 text-[10px] leading-snug">
                    <span className="font-semibold">{d.topic}</span>
                    <div className="text-muted-foreground">{d.detail}</div>
                    <span className="text-[9px] font-medium text-primary">{d.bias}</span>
                  </div>
                ))}
              </Section>
            )}

            {data.events?.length > 0 && (
              <Section title="Events (2 Days)">
                {data.events.slice(0, 6).map((ev, i) => (
                  <div key={i} className="flex items-center justify-between py-0.5 text-[10px]">
                    <span className="min-w-0 truncate pr-1">{ev.title}</span>
                    <span className="shrink-0 text-muted-foreground tabular-nums">{ev.date?.slice(5, 16).replace('T', ' ')}</span>
                  </div>
                ))}
              </Section>
            )}

            {data.news?.length > 0 && (
              <Section title="Headlines">
                {data.news.slice(0, 4).map((n, i) => (
                  <a key={i} href={n.link} target="_blank" rel="noreferrer" className="block py-0.5 text-[10px] leading-snug text-foreground hover:text-primary hover:underline">
                    <span className="text-muted-foreground">{n.source}: </span>{n.title}
                  </a>
                ))}
              </Section>
            )}
          </>
        )}
      </div>

      <div className="border-t border-border px-2 py-1 text-[9px] text-muted-foreground">
        TV scanner + NSE data · refreshed every 60s
      </div>
    </PanelShell>
  )
}
