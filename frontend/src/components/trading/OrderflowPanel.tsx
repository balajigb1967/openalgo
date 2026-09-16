import { useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { orderflowApi, type OrderflowBar, type OrderflowDetail, type OrderflowRow } from '@/api/scalper-orderflow'
import { cn } from '@/lib/utils'

/**
 * Orderflow Table side panel.
 *
 * Ported from fno-trader-pro's orderflow widget: timewise aggressive
 * Buy/Sell volume, intra-bar delta, CVD and diagonal imbalances per candle,
 * plus POC / value-area summary. Orderflow math runs on the backend
 * (services/orderflow_service.py); indices and MCX roots auto-resolve to their
 * near-month futures contract.
 */

const REFRESH_MS = 30_000
const TFS = ['1m', '3m', '5m', '15m', '30m', '1h']

function deltaColor(v: number): string {
  if (v > 0) return 'text-emerald-600 dark:text-emerald-400'
  if (v < 0) return 'text-rose-600 dark:text-rose-400'
  return 'text-muted-foreground'
}

function biasBadge(bias?: string): string {
  if (!bias) return ''
  if (bias.startsWith('BULLISH')) return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30'
  if (bias.startsWith('BEARISH')) return 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30'
  return 'bg-muted text-muted-foreground border-border'
}

function BarsTable({ bars }: { bars: OrderflowBar[] }) {
  return (
    <table className="w-full text-[10px] tabular-nums">
      <thead className="sticky top-0 bg-background/95 backdrop-blur">
        <tr className="text-muted-foreground">
          <th className="px-1 py-0.5 text-left font-medium">Time</th>
          <th className="px-1 py-0.5 text-right font-medium">C</th>
          <th className="px-1 py-0.5 text-right font-medium">Vol</th>
          <th className="px-1 py-0.5 text-right font-medium">Buy%</th>
          <th className="px-1 py-0.5 text-right font-medium">Sell%</th>
          <th className="px-1 py-0.5 text-right font-medium">Delta</th>
          <th className="px-1 py-0.5 text-right font-medium">CVD</th>
          <th className="px-1 py-0.5 text-left font-medium">Imb</th>
        </tr>
      </thead>
      <tbody>
        {bars.map((b, i) => (
          <tr key={i} className={cn('border-b border-border/40', b.is_stacked && 'bg-amber-500/5')}>
            <td className="px-1 py-0.5 text-muted-foreground">{b.time}</td>
            <td className="px-1 py-0.5 text-right">{b.close.toLocaleString('en-IN')}</td>
            <td className="px-1 py-0.5 text-right text-muted-foreground">{b.volume.toLocaleString('en-IN')}</td>
            <td className={cn('px-1 py-0.5 text-right', deltaColor(b.buy_pct - b.sell_pct))}>{b.buy_pct}</td>
            <td className={cn('px-1 py-0.5 text-right', deltaColor(b.buy_pct - b.sell_pct))}>{b.sell_pct}</td>
            <td className={cn('px-1 py-0.5 text-right font-medium', deltaColor(b.delta))}>{b.delta.toLocaleString('en-IN')}</td>
            <td className={cn('px-1 py-0.5 text-right', deltaColor(b.cvd))}>{b.cvd.toLocaleString('en-IN')}</td>
            <td className="px-1 py-0.5 text-left text-[9px]">{b.imbalance_label}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function OrderflowPanel({ activeSymbol }: { apiKey: string; activeSymbol: string | null }) {
  const [mode, setMode] = useState<'table' | 'detail'>('table')
  const [tf, setTf] = useState('5m')
  const [rows, setRows] = useState<OrderflowRow[]>([])
  const [detail, setDetail] = useState<OrderflowDetail | null>(null)
  const [symbol, setSymbol] = useState<string>(activeSymbol ?? 'NSE:NIFTY 50')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Adopt the focused chart's symbol when the pane changes
  useEffect(() => {
    if (activeSymbol) setSymbol(activeSymbol)
  }, [activeSymbol])

  const load = async () => {
    try {
      setError(null)
      if (mode === 'table') {
        const res = await orderflowApi.getTable(tf)
        setRows(res.rows ?? [])
      } else {
        const res = await orderflowApi.getDetail(symbol, tf, 25)
        setDetail(res)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load orderflow')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => clearInterval(t)
  }, [mode, tf, symbol])

  const s = detail?.summary

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="text-xs font-semibold text-foreground">Orderflow</div>
        <div className="flex items-center gap-1">
          <button type="button" onClick={load} className="rounded p-1 hover:bg-accent" title="Refresh">
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </button>
        </div>
      </div>

      <div className="flex items-center gap-1 border-b border-border px-2 py-1">
        <div className="flex rounded border border-border text-[10px]">
          {(['table', 'detail'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={cn('px-1.5 py-0.5 capitalize', mode === m ? 'bg-primary text-primary-foreground' : 'text-muted-foreground')}
            >
              {m}
            </button>
          ))}
        </div>
        <select
          value={tf}
          onChange={(e) => setTf(e.target.value)}
          className="rounded border border-border bg-transparent px-1 py-0.5 text-[10px]"
        >
          {TFS.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        {mode === 'detail' && (
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="MCX:CRUDEOIL"
            className="min-w-0 flex-1 rounded border border-border bg-transparent px-1 py-0.5 text-[10px]"
          />
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {error && <div className="m-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>}

        {mode === 'table' && (
          <table className="w-full text-[10px] tabular-nums">
            <thead className="sticky top-0 bg-background/95 backdrop-blur">
              <tr className="text-muted-foreground">
                <th className="px-1.5 py-1 text-left font-medium">Root</th>
                <th className="px-1.5 py-1 text-right font-medium">LTP</th>
                <th className="px-1.5 py-1 text-right font-medium">Chg%</th>
                <th className="px-1.5 py-1 text-right font-medium">Δ</th>
                <th className="px-1.5 py-1 text-right font-medium">CVD</th>
                <th className="px-1.5 py-1 text-left font-medium">Bias</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.key}
                  className="cursor-pointer border-b border-border/40 hover:bg-accent/50"
                  onClick={() => { setSymbol(`${r.market}:${r.key}`); setMode('detail') }}
                  title={`Open ${r.key} orderflow`}
                >
                  <td className="px-1.5 py-1 font-medium">{r.name}</td>
                  <td className="px-1.5 py-1 text-right">{r.ltp?.toLocaleString('en-IN') ?? '—'}</td>
                  <td className={cn('px-1.5 py-1 text-right', deltaColor(r.chp ?? 0))}>{r.chp !== null && r.chp !== undefined ? `${r.chp >= 0 ? '+' : ''}${r.chp.toFixed(2)}%` : '—'}</td>
                  <td className={cn('px-1.5 py-1 text-right', deltaColor(r.session_delta ?? 0))}>{r.session_delta?.toLocaleString('en-IN') ?? '—'}</td>
                  <td className={cn('px-1.5 py-1 text-right', deltaColor(r.session_cvd ?? 0))}>{r.session_cvd?.toLocaleString('en-IN') ?? '—'}</td>
                  <td className="px-1.5 py-1">
                    <span className={cn('rounded border px-1 py-px text-[9px]', biasBadge(r.delta_bias))}>
                      {r.delta_bias?.split(' ')[0] ?? '—'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {mode === 'detail' && detail && s && (
          <div>
            <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-2 py-1 text-[10px]">
              <span className="font-semibold">{detail.name}</span>
              <span className="tabular-nums">{s.ltp.toLocaleString('en-IN')}</span>
              <span className={cn('tabular-nums', deltaColor(s.chp))}>{s.chp >= 0 ? '+' : ''}{s.chp.toFixed(2)}%</span>
              <span className={cn('rounded border px-1 py-px', biasBadge(s.delta_bias))}>{s.delta_bias}</span>
              <span className="text-muted-foreground">{detail.target_symbol}</span>
            </div>
            <div className="grid grid-cols-3 gap-px border-b border-border bg-border/40 text-[10px]">
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">Session Δ</div><div className={cn('font-semibold tabular-nums', deltaColor(s.session_delta))}>{s.session_delta.toLocaleString('en-IN')}</div></div>
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">CVD</div><div className={cn('font-semibold tabular-nums', deltaColor(s.session_cvd))}>{s.session_cvd.toLocaleString('en-IN')}</div></div>
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">Volume</div><div className="font-semibold tabular-nums">{s.total_volume.toLocaleString('en-IN')}</div></div>
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">POC</div><div className="font-semibold tabular-nums">{s.poc.toLocaleString('en-IN')}</div></div>
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">VAH</div><div className="font-semibold tabular-nums">{s.vah.toLocaleString('en-IN')}</div></div>
              <div className="bg-background px-2 py-1"><div className="text-muted-foreground">VAL</div><div className="font-semibold tabular-nums">{s.val.toLocaleString('en-IN')}</div></div>
            </div>
            <BarsTable bars={detail.bars ?? []} />
          </div>
        )}

        {mode === 'detail' && !detail && !error && !loading && (
          <div className="p-2 text-[11px] text-muted-foreground">Enter a symbol like <code>MCX:CRUDEOIL</code> or <code>NSE:NIFTY 50</code>.</div>
        )}
      </div>

      <div className="border-t border-border px-2 py-1 text-[9px] text-muted-foreground">
        Buy/Sell split is a model estimate from candle structure.
      </div>
    </div>
  )
}
