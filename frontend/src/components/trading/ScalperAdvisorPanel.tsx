import { useEffect, useState } from 'react'
import { Loader2, RefreshCw, X } from 'lucide-react'
import {
  scalperApi,
  type ScalperAdvice,
  type ScalperAlert,
  type ScalperAdvisorResponse,
} from '@/api/scalper-orderflow'
import { cn } from '@/lib/utils'

/**
 * Scalper Advisor side panel.
 *
 * Ported from fno-trader-pro's scalper advisor widget: option-buying advisories
 * per instrument (chain structure + momentum + flows) with a live intraday
 * alert list, monitor events and a reversal-risk badge on open alerts. The
 * advisory math runs on the backend (services/scalper_advisor_service.py);
 * this panel renders it and issues arm/close actions.
 */

const REFRESH_MS = 20_000

function signalColor(signal: string): string {
  if (signal === 'BUY CE') return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30'
  if (signal === 'BUY PE') return 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30'
  return 'bg-muted text-muted-foreground border-border'
}

function severityColor(sev: string): string {
  switch (sev) {
    case 'DANGER': return 'text-rose-600 dark:text-rose-400'
    case 'WARN': return 'text-amber-600 dark:text-amber-400'
    case 'SUCCESS': return 'text-emerald-600 dark:text-emerald-400'
    default: return 'text-muted-foreground'
  }
}

function riskColor(label: string): string {
  if (label === 'REVERSAL LIKELY') return 'bg-rose-500/15 text-rose-600 dark:text-rose-400'
  if (label === 'RISK BUILDING') return 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
  return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
}

function fmtPct(v?: number | null): string {
  if (v === null || v === undefined) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`
}

function AdviceCard({
  adv,
  onArm,
  onDisarm,
  busy,
}: {
  adv: ScalperAdvice
  onArm: (key: string) => void
  onDisarm: (key: string) => void
  busy: string | null
}) {
  const [open, setOpen] = useState(false)
  const isBuy = adv.signal === 'BUY CE' || adv.signal === 'BUY PE'
  const pnl = adv.armed_pnl_pct
  const isBusy = busy === adv.key
  return (
    <div className={cn('rounded-md border bg-card/50 px-2 py-1.5', adv.armed ? 'border-primary/50' : 'border-border')}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {adv.armed && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-500" title="Monitor armed" />}
          <span className="text-[11px] font-semibold text-foreground truncate">{adv.name}</span>
          {adv.spot !== null && (
            <span className="text-[10px] text-muted-foreground tabular-nums">
              {adv.spot.toLocaleString('en-IN')}
            </span>
          )}
        </div>
        <span className={cn('shrink-0 rounded border px-1.5 py-px text-[10px] font-bold', signalColor(adv.signal))}>
          {adv.signal}
        </span>
      </div>

      {isBuy && (
        <div className="mt-1 grid grid-cols-4 gap-x-2 text-[10px] tabular-nums text-muted-foreground">
          <span className="truncate" title={adv.option_symbol ?? ''}>{adv.option_symbol}</span>
          <span>₹{adv.entry_premium?.toFixed(1)}</span>
          <span className="text-emerald-600 dark:text-emerald-400">₹{adv.target_premium?.toFixed(1)}</span>
          <span className="text-rose-600 dark:text-rose-400">₹{adv.sl_premium?.toFixed(1)}</span>
        </div>
      )}

      <div className="mt-1 flex items-center justify-between gap-2 text-[10px]">
        <div className="flex items-center gap-2 text-muted-foreground">
          <span>conf {adv.confidence}%</span>
          {adv.pcr !== null && <span>PCR {adv.pcr?.toFixed(2)}</span>}
          {adv.dte !== null && <span>{adv.dte}d</span>}
          {adv.momentum?.trend && adv.momentum.trend !== 'FLAT' && (
            <span className={adv.momentum.trend === 'UP' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>
              {adv.momentum.trend}
            </span>
          )}
        </div>
        {pnl !== undefined && (
          <span className={cn('font-semibold tabular-nums', (pnl ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
            {fmtPct(pnl)}
          </span>
        )}
      </div>

      {/* Armed position live strip: entry → current vs target / SL */}
      {adv.armed && (
        <div className="mt-1 flex items-center justify-between rounded bg-primary/5 px-1.5 py-0.5 text-[10px] tabular-nums">
          <span className="text-muted-foreground">
            LIVE ₹{adv.entry_premium?.toFixed(1)} → <b className="text-foreground">₹{adv.current_premium?.toFixed(1)}</b>
          </span>
          <span className="text-emerald-600 dark:text-emerald-400">T ₹{adv.armed_target_premium?.toFixed(1)}</span>
          <span className="text-rose-600 dark:text-rose-400">SL ₹{adv.armed_sl_premium?.toFixed(1)}</span>
        </div>
      )}

      <div className="mt-1 flex items-center justify-between gap-1">
        {isBuy ? (
          adv.armed ? (
            <button
              type="button"
              disabled={isBusy}
              onClick={() => onDisarm(adv.key)}
              className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-600 hover:bg-amber-500/20 disabled:opacity-50 dark:text-amber-400"
              title="Release the position monitor (stops tracking)"
            >
              {isBusy ? '…' : '◼ DISARM'}
            </button>
          ) : (
            <button
              type="button"
              disabled={isBusy}
              onClick={() => onArm(adv.key)}
              className="rounded border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-600 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-400"
              title="Arm live monitoring: target/SL hits, trailing, reversal alerts"
            >
              {isBusy ? '…' : '▶ ARM'}
            </button>
          )
        ) : (
          <span className="text-[10px] text-muted-foreground">{adv.note?.slice(0, 44) || '—'}</span>
        )}
        {(adv.basis?.length > 0 || adv.reversal_risk) && (
          <button type="button" onClick={() => setOpen((v) => !v)} className="text-[10px] text-muted-foreground hover:text-foreground underline-offset-1 hover:underline">
            {open ? 'Hide' : 'Details'}
          </button>
        )}
      </div>

      {open && (
        <div className="mt-1 space-y-0.5 border-t border-border pt-1">
          {adv.reversal_risk && adv.armed && (
            <div className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', riskColor(adv.reversal_risk.label))}>
              {adv.reversal_risk.label} — risk {adv.reversal_risk.score}/100 ({adv.reversal_risk.action})
            </div>
          )}
          {adv.basis?.map((b, i) => (
            <div key={i} className="text-[10px] text-muted-foreground leading-snug">• {b}</div>
          ))}
        </div>
      )}
    </div>
  )
}

export function ScalperAdvisorPanel(_props: { apiKey: string }) {
  const [data, setData] = useState<ScalperAdvisorResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'signals' | 'alerts' | 'events'>('signals')
  const [busy, setBusy] = useState<string | null>(null)

  const load = async (
    refresh = false,
    action?: { arm?: string; disarm?: string; armAlertId?: string }
  ) => {
    try {
      setError(null)
      const res = await scalperApi.getAdvisor(refresh, action)
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load advisor')
    } finally {
      setLoading(false)
    }
  }

  const handleArm = async (key: string) => {
    setBusy(key)
    try {
      await load(true, { arm: key })
    } finally {
      setBusy(null)
    }
  }

  const handleDisarm = async (key: string) => {
    setBusy(key)
    try {
      await load(true, { disarm: key })
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(() => load(), REFRESH_MS)
    return () => clearInterval(t)
  }, [])

  const closeAlert = async (a: ScalperAlert) => {
    try {
      await scalperApi.closeAlert(a.id, 'Manual close from sidebar')
      await load(true)
    } catch {
      /* the next poll re-syncs state anyway */
    }
  }

  const alerts: ScalperAlert[] = data?.monitor?.alerts ?? []
  const activeAlerts = alerts.filter((a) => a.status === 'ACTIVE')
  const events = data?.monitor?.events ?? []

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="text-xs font-semibold text-foreground">Scalper Advisor</div>
        <div className="flex items-center gap-1">
          {data && <span className="text-[10px] text-muted-foreground tabular-nums">{data.active_signals} live</span>}
          <button type="button" onClick={() => load(true)} className="rounded p-1 hover:bg-accent" title="Force refresh">
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </button>
        </div>
      </div>

      <div className="flex border-b border-border text-[11px]">
        {(['signals', 'alerts', 'events'] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={cn(
              'flex-1 px-2 py-1 capitalize',
              tab === t ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'
            )}
          >
            {t}
            {t === 'alerts' && activeAlerts.length > 0 && ` (${activeAlerts.length})`}
          </button>
        ))}
      </div>

      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {error && <div className="rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>}
        {data?.error && <div className="rounded bg-amber-500/10 px-2 py-1 text-[11px] text-amber-600 dark:text-amber-400">{data.error}</div>}

        {tab === 'signals' &&
          (data?.instruments ?? []).map((adv) => (
            <AdviceCard key={adv.key} adv={adv} onArm={handleArm} onDisarm={handleDisarm} busy={busy} />
          ))}

        {tab === 'alerts' &&
          (alerts.length === 0 ? (
            <div className="p-2 text-[11px] text-muted-foreground">No intraday alerts yet.</div>
          ) : (
            alerts.map((a) => (
              <div key={a.id} className="rounded-md border border-border bg-card/50 px-2 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className={cn('rounded border px-1 py-px text-[10px] font-bold', signalColor(`BUY ${a.side}`))}>
                      {a.side}
                    </span>
                    <span className="text-[11px] font-semibold truncate">{a.key}</span>
                    <span className="text-[10px] text-muted-foreground truncate">{a.option_symbol}</span>
                  </div>
                  {a.status === 'ACTIVE' ? (
                    <button type="button" onClick={() => closeAlert(a)} className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-destructive" title="Close alert">
                      <X className="h-3 w-3" />
                    </button>
                  ) : (
                    <span className={cn('text-[10px] font-bold', a.close_outcome === 'WIN' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
                      {a.close_outcome} {fmtPct(a.close_pnl_pct)}
                    </span>
                  )}
                </div>
                <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground tabular-nums">
                  <span>entry ₹{a.entry_premium?.toFixed(1)} · now ₹{a.current_premium?.toFixed(1)}</span>
                  {a.status === 'ACTIVE' && (
                    <span className={cn('font-semibold', (a.pnl_pct ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
                      {fmtPct(a.pnl_pct)}
                    </span>
                  )}
                </div>
                {a.armed && a.reversal_risk && (
                  <div className={cn('mt-1 rounded px-1.5 py-0.5 text-[10px] font-medium', riskColor(a.reversal_risk.label))}>
                    {a.reversal_risk.label} — risk {a.reversal_risk.score}/100
                  </div>
                )}
                {a.status === 'ACTIVE' && !a.armed && (
                  <button
                    type="button"
                    onClick={async () => {
                      setBusy(a.key)
                      try {
                        await load(true, { arm: a.key, armAlertId: a.id })
                        setTab('events')
                      } finally {
                        setBusy(null)
                      }
                    }}
                    disabled={busy === a.key}
                    className="mt-1 w-full rounded border border-emerald-500/40 bg-emerald-500/10 px-1 py-0.5 text-[10px] font-semibold text-emerald-600 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-400"
                    title="Arm live monitoring for this alert"
                  >
                    ▶ ARM MONITOR
                  </button>
                )}
              </div>
            ))
          ))}

        {tab === 'events' &&
          (events.length === 0 ? (
            <div className="p-2 text-[11px] text-muted-foreground">No monitor events yet. Arm a signal to start tracking.</div>
          ) : (
            events.map((ev, i) => (
              <div key={i} className="border-b border-border/60 pb-1">
                <div className="flex items-center gap-1.5 text-[10px]">
                  <span className="text-muted-foreground tabular-nums">{ev.ts}</span>
                  <span className={cn('font-semibold', severityColor(ev.severity))}>{ev.key}</span>
                </div>
                <div className="text-[10px] text-foreground leading-snug">{ev.msg}</div>
              </div>
            ))
          ))}
      </div>

      <div className="border-t border-border px-2 py-1 text-[9px] text-muted-foreground">
        Advisory only — option buying is high risk.
      </div>
    </div>
  )
}
