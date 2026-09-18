import { useEffect, useState } from 'react'
import { Loader2, RefreshCw, X } from 'lucide-react'
import {
  scalperApi,
  type ScalperAdvice,
  type ScalperAlert,
  type ScalperAdvisorResponse,
  type ScalperArmedPos,
} from '@/api/scalper-orderflow'
import { PanelShell } from './panelShell'
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

function fmtClock(t: number): string {
  return new Date(t * 1000).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Kolkata',
  })
}

/** Small live premium sparkline for an armed position (chronological series). */
function PremiumSpark({ series, target, sl, height = 34 }: {
  series: Array<{ t: number; p: number }>
  target?: number | null
  sl?: number | null
  height?: number
}) {
  if (series.length < 2) {
    return <div className="rounded bg-muted/40 px-1.5 py-1 text-[9px] text-muted-foreground">Collecting premium trail…</div>
  }
  const pts = series.map((s) => s.p)
  const levels = [target, sl].filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  const all = [...pts, ...levels]
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const W = 100
  const path = series.map((s, i) => `${i === 0 ? 'M' : 'L'}${((s.p - min) / span * W).toFixed(2)},${((1 - (s.p - min) / span) * height).toFixed(2)}`).join(' ')
  const last = pts[pts.length - 1]
  const first = pts[0]
  const up = last >= first
  return (
    <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" className="h-[34px] w-full">
      {typeof target === 'number' && Number.isFinite(target) && (
        <line x1={0} x2={W} y1={(1 - (target - min) / span) * height} y2={(1 - (target - min) / span) * height} stroke="rgb(16 185 129 / 0.55)" strokeWidth={0.6} strokeDasharray="2 2" />
      )}
      {typeof sl === 'number' && Number.isFinite(sl) && (
        <line x1={0} x2={W} y1={(1 - (sl - min) / span) * height} y2={(1 - (sl - min) / span) * height} stroke="rgb(244 63 94 / 0.55)" strokeWidth={0.6} strokeDasharray="2 2" />
      )}
      <path d={path} fill="none" stroke={up ? 'rgb(16 185 129)' : 'rgb(244 63 94)'} strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      <circle cx={(last - min) / span * W} cy={(1 - (last - min) / span) * height} r={1.4} fill={up ? 'rgb(16 185 129)' : 'rgb(244 63 94)'} />
    </svg>
  )
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

      {/* Armed position live strip + premium trail */}
      {adv.armed && (
        <div className="mt-1 rounded bg-primary/5 px-1.5 py-1">
          <div className="flex items-center justify-between text-[10px] tabular-nums">
            <span className="text-muted-foreground">
              LIVE ₹{adv.entry_premium?.toFixed(1)} → <b className="text-foreground">₹{adv.current_premium?.toFixed(1)}</b>
            </span>
            <span className="text-emerald-600 dark:text-emerald-400">T ₹{adv.armed_target_premium?.toFixed(1)}</span>
            <span className="text-rose-600 dark:text-rose-400">SL ₹{adv.armed_sl_premium?.toFixed(1)}</span>
          </div>
          {adv.premium_series && adv.premium_series.length > 0 && (
            <div className="mt-1">
              <PremiumSpark series={adv.premium_series} target={adv.armed_target_premium} sl={adv.armed_sl_premium} />
            </div>
          )}
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

/** Live monitor tab — armed positions with trail/P&L/revisions + active intraday alerts. */
function MonitorTab({ armedMap, alerts, onDisarm, onCloseAlert, busy }: {
  armedMap: Record<string, ScalperArmedPos>
  alerts: ScalperAlert[]
  onDisarm: (key: string) => void
  onCloseAlert: (a: ScalperAlert) => void
  busy: string | null
}) {
  const entries = Object.entries(armedMap) as Array<[string, ScalperArmedPos]>
  const activeAlerts = alerts.filter((a) => a.status === 'ACTIVE')
  if (!entries.length && !activeAlerts.length) {
    return (
      <div className="p-2 text-[11px] text-muted-foreground">
        Nothing live-monitored yet. Arm a signal (or turn on AUTO) to start tracking.
      </div>
    )
  }
  return (
    <>
      {activeAlerts.length > 0 && (
        <div className="text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">Intraday alerts ({activeAlerts.length})</div>
      )}
      {activeAlerts.map((a) => (
        <div key={a.id} className="rounded-md border border-border bg-card/50 px-2 py-1">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5">
              {a.armed && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-500" />}
              <span className="truncate text-[10px] font-semibold text-foreground">{a.name || a.key}</span>
              <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] font-bold', signalColor(`BUY ${a.side}`))}>{a.side}</span>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <span className={cn('text-[10px] font-semibold tabular-nums', (a.pnl_pct ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
                {fmtPct(a.pnl_pct)}
              </span>
              <button type="button" onClick={() => onCloseAlert(a)} className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-destructive" title="Close alert">
                <X className="h-2.5 w-2.5" />
              </button>
            </div>
          </div>
          <div className="mt-0.5 flex items-center justify-between text-[9px] text-muted-foreground tabular-nums">
            <span className="truncate" title={a.option_symbol}>{a.option_symbol}</span>
            <span>entry ₹{a.entry_premium?.toFixed(1)} · now ₹{a.current_premium?.toFixed(1)}</span>
          </div>
          {a.armed && a.reversal_risk && (
            <div className={cn('mt-0.5 rounded px-1.5 py-0.5 text-[9px] font-medium', riskColor(a.reversal_risk.label))}>
              {a.reversal_risk.label} — risk {a.reversal_risk.score}/100
            </div>
          )}
        </div>
      ))}
      {entries.length > 0 && (
        <div className="pt-0.5 text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">Armed positions ({entries.length})</div>
      )}
      {entries.map(([key, p]) => {
        const pnl = p.pnl_pct ?? 0
        const trail = p.trail_done ? 'TRAILING' : p.be_done ? 'BREAKEVEN' : 'INITIAL'
        return (
          <div key={key} className="rounded-md border border-primary/40 bg-card/50 px-2 py-1.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-500" title="Live monitoring" />
                <span className="truncate text-[11px] font-semibold text-foreground">{p.name || key}</span>
                {p.side && (
                  <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] font-bold', signalColor(`BUY ${p.side}`))}>
                    {p.side}
                  </span>
                )}
              </div>
              <button
                type="button"
                disabled={busy === key}
                onClick={() => onDisarm(key)}
                className="shrink-0 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-px text-[9px] font-semibold text-amber-600 hover:bg-amber-500/20 disabled:opacity-50 dark:text-amber-400"
                title="Release monitor"
              >
                {busy === key ? '…' : '◼ DISARM'}
              </button>
            </div>
            <div className="mt-0.5 truncate text-[9px] text-muted-foreground" title={p.option_symbol}>
              {p.option_symbol} · armed {p.armed_at ?? '—'} · trail {trail}
            </div>
            <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums">
              <span className="text-muted-foreground">
                ₹{p.entry_premium?.toFixed(1)} → <b className="text-foreground">₹{p.current_premium?.toFixed(1)}</b>
                {typeof p.hi_premium === 'number' && <span className="ml-1 text-emerald-600/70 dark:text-emerald-400/70">H ₹{p.hi_premium.toFixed(1)}</span>}
              </span>
              <span className={cn('font-semibold', pnl >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')}>
                {fmtPct(pnl)}
              </span>
            </div>
            {p.reversal_risk && (
              <div className={cn('mt-1 rounded px-1.5 py-0.5 text-[9px] font-medium', riskColor(p.reversal_risk.label))}>
                {p.reversal_risk.label} — risk {p.reversal_risk.score}/100
              </div>
            )}
            {p.premium_series && p.premium_series.length > 1 && (
              <div className="mt-1">
                <PremiumSpark series={p.premium_series} target={p.target_premium} sl={p.sl_premium} />
                <div className="mt-0.5 flex justify-between text-[8px] text-muted-foreground tabular-nums">
                  <span>{fmtClock(p.premium_series[0].t)}</span>
                  <span>{fmtClock(p.premium_series[p.premium_series.length - 1].t)}</span>
                </div>
              </div>
            )}
            {(p.revision_log?.length ?? 0) > 0 && (
              <div className="mt-1 space-y-0.5 border-t border-border/60 pt-1">
                {p.revision_log!.slice(-3).map((r, i) => (
                  <div key={i} className="text-[9px] leading-snug text-muted-foreground">
                    <span className="tabular-nums">{r.ts}</span> — {r.msg}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}

export function ScalperAdvisorPanel({ activeSymbol }: { apiKey: string; activeSymbol?: string | null }) {
  const [data, setData] = useState<ScalperAdvisorResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'signals' | 'monitor' | 'events'>('signals')
  const [busy, setBusy] = useState<string | null>(null)
  const [autoArm, setAutoArm] = useState<boolean>(() => localStorage.getItem('oa-scalper-autoarm') === '1')

  const load = async (
    refresh = false,
    action?: { arm?: string; disarm?: string; armAlertId?: string }
  ) => {
    try {
      setError(null)
      const res = await scalperApi.getAdvisor(refresh, action, autoArm, activeSymbol ?? null)
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load advisor')
    } finally {
      setLoading(false)
    }
  }

  const toggleAutoArm = async () => {
    const next = !autoArm
    setAutoArm(next)
    localStorage.setItem('oa-scalper-autoarm', next ? '1' : '0')
    await load(true)
  }

  const closeAlert = async (a: ScalperAlert) => {
    try {
      await scalperApi.closeAlert(a.id, 'Manual close from sidebar')
      await load(true)
    } catch {
      /* the next poll re-syncs state anyway */
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoArm, activeSymbol])

  const alerts: ScalperAlert[] = data?.monitor?.alerts ?? []
  const events = data?.monitor?.events ?? []
  const armedList: number = Object.keys(data?.monitor?.armed ?? {}).length

  return (
    <PanelShell id="oa-panel-scalper" label="Scalper Advisor" storageKey="oa-trading-scalper-width" defaultWidth={340}>
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="flex items-center gap-1.5">
          <div className="text-xs font-semibold text-foreground">Scalper Advisor</div>
          {autoArm && (
            <span className="flex items-center gap-1 rounded bg-emerald-500/10 px-1 py-px text-[9px] font-semibold text-emerald-600 dark:text-emerald-400" title="Every fresh BUY signal is live-monitored automatically">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" /> AUTO
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {data && <span className="text-[10px] text-muted-foreground tabular-nums">{data.active_signals} live</span>}
          <button
            type="button"
            onClick={toggleAutoArm}
            className={cn(
              'rounded border px-1.5 py-0.5 text-[9px] font-semibold',
              autoArm
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                : 'border-border text-muted-foreground hover:text-foreground'
            )}
            title="Automatically live-monitor every fresh BUY signal (target/SL/trailing/reversal) without manual arming"
          >
            {autoArm ? 'AUTO ON' : 'AUTO'}
          </button>
          <button type="button" onClick={() => load(true)} className="rounded p-1 hover:bg-accent" title="Force refresh">
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </button>
        </div>
      </div>

      <div className="flex border-b border-border text-[11px]">
        {(['signals', 'monitor', 'events'] as const).map((t) => (
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
            {t === 'monitor' && armedList > 0 && ` (${armedList})`}
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

        {tab === 'monitor' && (
          <MonitorTab
            armedMap={data?.monitor?.armed ?? {}}
            alerts={alerts}
            onDisarm={handleDisarm}
            onCloseAlert={closeAlert}
            busy={busy}
          />
        )}

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
    </PanelShell>
  )
}
