/**
 * FCC AI panel — the fno-trader AI feature set on OpenAlgo's data.
 *
 * Three tabs, no overlap with the Assistant beside it in the rail:
 * - **Chat** is grounded with a live market snapshot the server builds from
 *   the platform's own quote / option-chain / candle services at send time,
 *   focused on the operator's active chart symbol.
 * - **Live** generates institutional-style commentary bullets for a symbol
 *   and keeps the recent history, so the operator can replay the session.
 * - **Agent** runs the fcc-* coding agents against this project, streamed.
 *
 * The panel speaks to the plugin blueprint at /plugins/fcc (the same surface
 * the phone uses), authenticated by the browser session.
 */

import { Loader2, Send, Sparkles, Square, Radio } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { PANEL_HEADER, PanelShell } from './panelShell'

interface FccStatus {
  connected: boolean
  models: string[]
  agents_available: Record<string, boolean>
  error: string | null
}

interface Turn {
  role: 'user' | 'assistant'
  content: string
  error?: boolean
}

interface CommentaryItem {
  id: string
  timestamp: string
  symbol: string
  headline: string
  commentary: string
  bias: string
  tag: string
  signals?: string[]
}

const API = '/plugins/fcc'

async function fccFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) throw new Error(`FCC ${res.status}`)
  return res.json() as Promise<T>
}

export function FccAiPanel({ activeSymbol }: { activeSymbol?: string | null }) {
  const focus = (activeSymbol ?? '').split(':').pop()?.trim() || ''

  const [status, setStatus] = useState<FccStatus | null>(null)
  const [tab, setTab] = useState<'chat' | 'live' | 'agent'>('chat')

  // chat
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState('')
  const threadRef = useRef<HTMLDivElement>(null)

  // live
  const [symbol, setSymbol] = useState('NIFTY')
  const [items, setItems] = useState<CommentaryItem[]>([])
  const [liveBusy, setLiveBusy] = useState(false)

  // agent
  const [agent, setAgent] = useState('')
  const [agentTask, setAgentTask] = useState('')
  const [agentModel, setAgentModel] = useState('')
  const [agentOutput, setAgentOutput] = useState('')
  const [agentRun, setAgentRun] = useState<{ id: string; status: string } | null>(null)

  // The active chart drives the panel: chat focus, commentary symbol and the
  // agent's starting instrument all follow the pane the operator is on.
  useEffect(() => {
    if (focus) setSymbol(focus)
  }, [focus])

  useEffect(() => {
    fccFetch<{ connected: boolean; models: string[]; agents_available: Record<string, boolean> }>('/status')
      .then((d) => setStatus({ ...d, error: null }))
      .catch((e) => setStatus({ connected: false, models: [], agents_available: {}, error: String(e) }))
  }, [])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [turns])

  const send = useCallback(async () => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    setTurns((t) => [...t, { role: 'user', content: text }, { role: 'assistant', content: '' }])
    setBusy(true)
    try {
      const history = [...turns, { role: 'user', content: text } as Turn]
        .filter((t) => !t.error)
        .map((t) => ({ role: t.role, content: t.content }))
      const d = await fccFetch<{ content: string }>('/chat', {
        method: 'POST',
        body: JSON.stringify({ messages: history, context: true, focus: focus || undefined, model: model || undefined }),
      })
      setTurns((t) => {
        const next = [...t]
        next[next.length - 1] = { role: 'assistant', content: d.content || '…' }
        return next
      })
    } catch (e) {
      setTurns((t) => {
        const next = [...t]
        next[next.length - 1] = { role: 'assistant', content: `FCC AI error: ${e}`, error: true }
        return next
      })
    } finally {
      setBusy(false)
    }
  }, [input, busy, turns, focus, model])

  const generateCommentary = useCallback(async (sym: string) => {
    setLiveBusy(true)
    try {
      const d = await fccFetch<{ item: CommentaryItem }>('/commentary', {
        method: 'POST',
        body: JSON.stringify({ symbol: sym }),
      })
      setItems((prev) => [d.item, ...prev])
    } catch {
      /* surfaced by the empty list */
    } finally {
      setLiveBusy(false)
    }
  }, [])

  const runAgent = useCallback(async () => {
    const task = agentTask.trim()
    if (!agent || !task || agentRun?.status === 'running') return
    setAgentTask('')
    try {
      const d = await fccFetch<{ run: { run_id: string } }>('/agent', {
        method: 'POST',
        body: JSON.stringify({ agent, prompt: task, model: agentModel || undefined, symbol: focus || undefined }),
      })
      setAgentRun({ id: d.run.run_id, status: 'running' })
      setAgentOutput('')
      const es = new EventSource(`${API}/agent/${d.run.run_id}/stream`)
      es.onmessage = (ev) => {
        const f = JSON.parse(ev.data) as { status: string; output: string; error?: string }
        setAgentOutput(f.output || '')
        setAgentRun({ id: d.run.run_id, status: f.status })
        if (f.status !== 'running') es.close()
      }
      es.onerror = () => es.close()
    } catch (e) {
      setAgentOutput(`Launch failed: ${e}`)
    }
  }, [agent, agentTask, agentModel, focus, agentRun])

  const stopAgent = useCallback(async () => {
    if (!agentRun) return
    try {
      await fccFetch(`/agent/${agentRun.id}/stop`, { method: 'POST', body: '{}' })
    } catch { /* best effort */ }
  }, [agentRun])

  const agents = status ? Object.entries(status.agents_available).filter(([, ok]) => ok).map(([a]) => a) : []
  const models = status?.models ?? []

  const modelDatalist = (
    <datalist id="fcc-models">
      {models.slice(0, 400).map((m) => <option key={m} value={m} />)}
    </datalist>
  )

  return (
    <PanelShell id="oa-panel-fcc" label="FCC AI" storageKey="oa-trading-fcc-width" defaultWidth={400}>
      <div className={PANEL_HEADER}>
        <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">FCC AI</span>
        {focus && (
          <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
            {focus}
          </span>
        )}
        {status && !status.connected && (
          <span className="shrink-0 text-[10px] font-medium text-destructive" title={status.error ?? ''}>
            offline
          </span>
        )}
        <div className="flex shrink-0 rounded-md border border-border text-[11px]">
          {(['chat', 'live', 'agent'] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={cn(
                'px-2 py-0.5 capitalize transition-colors first:rounded-l-md last:rounded-r-md',
                tab === t ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/60'
              )}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {tab === 'chat' && (
        <div ref={threadRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
          {turns.length === 0 && (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
              <Sparkles className="h-8 w-8 text-muted-foreground/50" aria-hidden />
              <p className="text-sm font-medium">
                {focus ? `Grounded on ${focus} + live OpenAlgo data` : 'Grounded in live OpenAlgo data'}
              </p>
              <p className="text-xs leading-relaxed text-muted-foreground">
                {focus
                  ? <>Quotes, candles, option chain and scalper state for <b>{focus}</b> are injected into every turn. Try &ldquo;analyse the chart&rdquo;.</>
                  : 'Open a chart to map a symbol, or just ask — quotes, chains and scalper state are injected into every turn.'}
              </p>
            </div>
          )}
          {turns.map((t, i) => (
            <div key={i} className={cn('max-w-[92%] rounded-lg border px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap',
              t.role === 'user' ? 'ml-auto border-primary/30 bg-primary/10' : 'border-border bg-accent/40',
              t.error && 'border-destructive/40 text-destructive')}>
              {t.content || '…'}
            </div>
          ))}
        </div>
      )}

      {tab === 'live' && (
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          <div className="mb-3 flex items-center gap-1.5">
            <Input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              className="h-8 flex-1 text-[12px]"
              placeholder="Symbol"
            />
            <Button size="sm" variant="secondary" className="h-8" disabled={liveBusy || !symbol.trim()}
              onClick={() => generateCommentary(symbol.trim())}>
              {liveBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Radio className="h-3.5 w-3.5" />}
              Squawk
            </Button>
          </div>
          {items.length === 0 && (
            <p className="px-1 text-xs text-muted-foreground">
              Deep squawk bullets: price action, OI buildup walls, ATM IV, pivots and scalper
              state — generated for any symbol; history builds up below.
            </p>
          )}
          <div className="space-y-2">
            {items.map((it) => (
              <div key={it.id} className={cn('rounded-md border-l-2 bg-accent/30 px-3 py-2',
                it.bias === 'BULLISH' ? 'border-l-green-500' : it.bias === 'BEARISH' ? 'border-l-red-500' : 'border-l-muted-foreground')}>
                <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                  <span>{it.timestamp} · {it.symbol}</span>
                  <span className="ml-auto font-semibold">{it.bias}</span>
                </div>
                <p className="mt-1 text-[12px] font-semibold">{it.headline}</p>
                <p className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">{it.commentary}</p>
                {it.signals && it.signals.length > 0 && (
                  <ul className="mt-1.5 space-y-0.5 border-t border-border/60 pt-1.5">
                    {it.signals.slice(0, 8).map((s, i) => (
                      <li key={i} className="text-[10.5px] leading-snug text-muted-foreground">• {s}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === 'agent' && (
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {status && !status.connected ? (
            <p className="text-xs text-muted-foreground">
              FCC proxy offline on the server ({status.error ?? 'unreachable'}).
            </p>
          ) : agents.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No FCC agents installed on the server. Install with{' '}
              <code className="rounded bg-accent px-1">npm i -g free-claude-code</code>.
            </p>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center gap-1.5">
                <select
                  value={agent || agents[0]}
                  onChange={(e) => setAgent(e.target.value)}
                  className="h-8 min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-[12px]"
                  aria-label="Coding agent"
                >
                  {agents.map((a) => <option key={a} value={a}>fcc-{a}</option>)}
                </select>
                <Input
                  value={agentModel}
                  onChange={(e) => setAgentModel(e.target.value)}
                  list="fcc-models"
                  placeholder="model (default)"
                  className="h-8 min-w-0 flex-1 text-[11px]"
                />
              </div>
              <div className="flex items-center gap-1.5">
                <Input
                  value={agentTask}
                  onChange={(e) => setAgentTask(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && runAgent()}
                  placeholder={focus ? `Task for fcc-${agent || agents[0]} (maps to ${focus})…` : `Task for fcc-${agent || agents[0]}…`}
                  className="h-8 flex-1 text-[12px]"
                />
                <Button size="sm" className="h-8" disabled={!agentTask.trim() || agentRun?.status === 'running'} onClick={runAgent}>
                  Run
                </Button>
              </div>
            </div>
          )}
          {agentRun && (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                <span className={cn('h-1.5 w-1.5 rounded-full',
                  agentRun.status === 'running' ? 'animate-pulse bg-primary' : 'bg-muted-foreground')} />
                run {agentRun.id} · {agentRun.status}
                {agentRun.status === 'running' && (
                  <button type="button" className="ml-auto text-destructive hover:underline" onClick={stopAgent}>
                    <Square className="mr-1 inline h-3 w-3" />stop
                  </button>
                )}
              </div>
              <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-md bg-accent/30 p-2 text-[11px] leading-relaxed">
                {agentOutput || '…'}
              </pre>
            </div>
          )}
        </div>
      )}

      {tab === 'chat' && (
        <div className="shrink-0 border-t border-border px-3 py-2.5">
          <div className="mb-1.5 flex items-center gap-1.5">
            <Input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              list="fcc-models"
              placeholder="model (server default)"
              className="h-7 flex-1 text-[11px]"
            />
          </div>
          {modelDatalist}
          <div className="flex items-center gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), send())}
              placeholder={busy ? 'FCC AI is thinking…' : focus ? `Ask about ${focus}…` : 'Ask the FCC AI…'}
              className="h-9 text-[13px]"
              disabled={busy}
            />
            <Button size="icon" className="h-9 w-9 shrink-0" disabled={busy || !input.trim()} onClick={send}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </Button>
          </div>
        </div>
      )}
    </PanelShell>
  )
}
