/**
 * FCC AI panel — the fno-trader AI feature set on OpenAlgo's data.
 *
 * Two tabs, no overlap with the Assistant beside it in the rail:
 * - **Chat** is grounded with a live market snapshot the server builds from
 *   the platform's own quote / option-chain / scalper services at send time.
 *   It complements (does not replace) the chart assistant: that one reads the
 *   pane and can draw; this one reads the whole platform.
 * - **Live** generates institutional-style commentary bullets for a symbol
 *   and keeps the recent history, so the operator can replay the session.
 *
 * The coding-agent runner lives here too — it is a power tool and the phone
 * ships the same one, so both surfaces stay at feature parity.
 */

import { Loader2, Send, Sparkles, Square, Radio, ListTree } from 'lucide-react'
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
}

const API = '/plugins/fcc'

async function fccFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    // Surface the server's actual error message (e.g. why a launch was
    // refused) instead of a bare status code.
    let msg = `FCC ${res.status}`
    try {
      const body = (await res.json()) as { message?: string }
      if (body?.message) msg = body.message
    } catch {
      /* keep the status-code fallback */
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

/** Strip ANSI escapes and node CLI warnings from streamed agent output. */
function cleanAgentOutput(raw: string): string {
  return raw
    // eslint-disable-next-line no-control-regex
    .replace(/\u001b\[[0-9;?]*[a-zA-Z]|\u001b\][^\u0007]*\u0007/g, '')
    .split('\n')
    .filter((line) => !/Warning:|--trace-warnings|^\(node:/.test(line))
    .join('\n')
}

export function FccAiPanel({ activeSymbol }: { activeSymbol?: string | null }) {
  const [status, setStatus] = useState<FccStatus | null>(null)
  const [tab, setTab] = useState<'chat' | 'live' | 'agent'>('live')

  // chat
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const threadRef = useRef<HTMLDivElement>(null)

  // live
  const [symbol, setSymbol] = useState(() => (activeSymbol?.split(':').pop() || 'NIFTY').toUpperCase())
  const [items, setItems] = useState<CommentaryItem[]>([])
  const [liveBusy, setLiveBusy] = useState(false)

  // agent
  const [agentOutput, setAgentOutput] = useState('')
  const [agentRun, setAgentRun] = useState<{ id: string; status: string } | null>(null)
  const [agentInput, setAgentInput] = useState('')
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [launching, setLaunching] = useState(false)

  useEffect(() => {
    fccFetch<{ connected: boolean; models: string[]; agents_available: Record<string, boolean> }>('/status')
      .then((d) => setStatus({ ...d, error: null }))
      .catch((e) => setStatus({ connected: false, models: [], agents_available: {}, error: String(e) }))
  }, [])

  // Follow activeSymbol changes and sync squawk on server
  useEffect(() => {
    if (activeSymbol) {
      const s = (activeSymbol.split(':').pop() || '').trim().toUpperCase()
      if (s) {
        setSymbol(s)
        fccFetch('/commentary/auto', {
          method: 'POST',
          body: JSON.stringify({ enabled: true, symbol: s }),
        })
          .then(() => {
            // Give the server 1.5s to generate the instant bullet for the new symbol, then fetch history
            setTimeout(() => {
              fccFetch<{ history: CommentaryItem[] }>('/commentary/history?limit=30')
                .then((d) => { if (d.history) setItems(d.history) })
                .catch(() => {})
            }, 1500)
          })
          .catch(() => {})
      }
    }
  }, [activeSymbol])

  // Poll live squawk history continuously
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const d = await fccFetch<{ history: CommentaryItem[] }>('/commentary/history?limit=30')
        if (alive && d.history) {
          setItems(d.history)
        }
      } catch {
        // ignore
      }
    }
    poll()
    const timer = setInterval(poll, 6000)
    return () => {
      alive = false
      clearInterval(timer)
    }
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
        body: JSON.stringify({
          messages: history,
          context: true,
          focus: symbol || (activeSymbol ? activeSymbol.split(':').pop() : undefined),
        }),
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
  }, [input, busy, turns, symbol, activeSymbol])

  const [scanBusy, setScanBusy] = useState(false)
  const scanWatchlist = useCallback(async () => {
    setScanBusy(true)
    try {
      const d = await fccFetch<{ items: CommentaryItem[] }>('/commentary/scan-watchlist', {
        method: 'POST',
        body: JSON.stringify({}),
      })
      if (d.items && d.items.length) {
        setItems((prev) => [...d.items, ...prev])
      }
    } catch {
      /* surfaced by the empty list */
    } finally {
      setScanBusy(false)
    }
  }, [])

  const generateCommentary = useCallback(async (sym: string) => {
    setLiveBusy(true)
    try {
      const d = await fccFetch<{ item: CommentaryItem }>('/commentary', {
        method: 'POST',
        body: JSON.stringify({ symbol: sym, force: true }),
      })
      if (d.item) {
        setItems((prev) => [d.item, ...prev.filter((p) => p.id !== d.item.id)])
      }
    } catch {
      /* surfaced by the empty list */
    } finally {
      setLiveBusy(false)
    }
  }, [])

  const runAgent = useCallback(async (agent: string, prompt: string) => {
    const task = prompt.trim()
    if (!task) return
    setLaunching(true)
    try {
      const d = await fccFetch<{ run: { run_id: string } }>('/agent', {
        method: 'POST',
        body: JSON.stringify({ agent, prompt: task }),
      })
      setAgentRun({ id: d.run.run_id, status: 'running' })
      setAgentOutput('')
      setAgentInput('')
      const es = new EventSource(`${API}/agent/${d.run.run_id}/stream`)
      es.onmessage = (ev) => {
        const f = JSON.parse(ev.data) as { status: string; output: string; error?: string }
        setAgentOutput(cleanAgentOutput(f.output || ''))
        setAgentRun({ id: d.run.run_id, status: f.status })
        if (f.status !== 'running') es.close()
      }
      es.onerror = () => es.close()
    } catch (e) {
      setAgentOutput(`Launch failed: ${e instanceof Error ? e.message : String(e)}`)
      setAgentRun(null)
    } finally {
      setLaunching(false)
    }
  }, [])

  const stopAgent = useCallback(async () => {
    if (!agentRun) return
    try {
      await fccFetch(`/agent/${agentRun.id}/stop`, { method: 'POST', body: '{}' })
    } catch { /* best effort */ }
  }, [agentRun])

  const agents = status ? Object.entries(status.agents_available).filter(([, ok]) => ok).map(([a]) => a) : []

  return (
    <PanelShell id="oa-panel-fcc" label="FCC AI" storageKey="oa-trading-fcc-width" defaultWidth={400}>
      <div className={PANEL_HEADER}>
        <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">FCC AI</span>
        {status && !status.connected && (
          <span className="shrink-0 text-[10px] font-medium text-destructive">offline</span>
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
              <p className="text-sm font-medium">Grounded in live OpenAlgo data</p>
              <p className="max-w-[240px] text-xs text-muted-foreground">
                Ask about the loaded symbol ({symbol}), watchlist breadth, options OI, or trading setups.
              </p>
            </div>
          )}
          {turns.map((t, idx) => (
            <div key={idx} className={cn(
              'rounded-lg px-3 py-2 text-xs leading-relaxed',
              t.role === 'user' ? 'ml-6 bg-accent/60 text-foreground' : 'mr-2 border border-border bg-card text-card-foreground',
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
            <Button size="sm" variant="secondary" className="h-8" disabled={liveBusy}
              onClick={() => generateCommentary(symbol)}>
              {liveBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Radio className="h-3.5 w-3.5" />}
              Squawk
            </Button>
            <Button size="sm" variant="outline" className="h-8" disabled={scanBusy}
              onClick={scanWatchlist} title="Scan Watchlist Symbols">
              {scanBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ListTree className="h-3.5 w-3.5" />}
              Scan List
            </Button>
          </div>
          <div className="mb-2.5 flex items-center justify-between rounded bg-emerald-500/10 px-2 py-1 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
              SQUAWK ALWAYS ON ({symbol})
            </span>
            <span className="text-[10px] text-muted-foreground font-normal">
              {items.length > 0 ? `Updated ${items[0].timestamp}` : 'Running live feed…'}
            </span>
          </div>
          {items.length === 0 && (
            <p className="px-1 text-xs text-muted-foreground">
              Generate a bullet for any symbol; history builds up below.
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
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === 'agent' && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
            {agents.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No FCC agents installed on the server. Install with{' '}
                <code className="rounded bg-accent px-1">npm i -g free-claude-code</code>.
              </p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {agents.map((a) => (
                  <button
                    key={a}
                    type="button"
                    onClick={() => setSelectedAgent(a)}
                    className={cn(
                      'rounded-md border px-2 py-1 text-[11px] transition-colors',
                      selectedAgent === a
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border text-muted-foreground hover:bg-accent/60'
                    )}
                  >
                    fcc-{a}
                  </button>
                ))}
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
          <div className="shrink-0 border-t border-border px-3 py-2.5">
            <div className="flex items-center gap-2">
              <Input
                value={agentInput}
                onChange={(e) => setAgentInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    if (selectedAgent && !launching && agentRun?.status !== 'running') {
                      runAgent(selectedAgent, agentInput)
                    }
                  }
                }}
                placeholder={selectedAgent
                  ? `Task for fcc-${selectedAgent}…`
                  : 'Pick an agent above, then type the task…'}
                className="h-9 text-[13px]"
                disabled={launching || agentRun?.status === 'running'}
              />
              <Button
                size="icon"
                className="h-9 w-9 shrink-0"
                disabled={!selectedAgent || launching || agentRun?.status === 'running' || !agentInput.trim()}
                onClick={() => selectedAgent && runAgent(selectedAgent, agentInput)}
              >
                {launching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              </Button>
            </div>
          </div>
        </div>
      )}

      {tab === 'chat' && (
        <div className="shrink-0 border-t border-border px-3 py-2.5">
          <div className="flex items-center gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), send())}
              placeholder={busy ? 'FCC AI is thinking…' : 'Ask the FCC AI…'}
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
