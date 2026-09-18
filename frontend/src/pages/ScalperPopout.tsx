import { lazy, Suspense, useEffect, useState } from 'react'

// The same terminal the /trading page embeds, pulled in lazily so opening the
// popout does not pay for the chart grid's other imports.
const ScalperTerminal = lazy(() =>
  import('@/components/scalping/ScalperTerminal').then((m) => ({ default: m.ScalperTerminal }))
)

/**
 * Standalone scalper terminal for the /trading page's popout button.
 *
 * A detached window instead of an overlay: the terminal keeps streaming on a
 * second monitor while the charts keep their full grid. It is the component,
 * not a copy of it — the same keys, the same websocket, the same order
 * plumbing — so behaviour cannot drift between the two surfaces.
 *
 * The window gets its credentials the way Trading.tsx does, from the session
 * endpoints rather than a URL parameter: an API key in the location bar leaks
 * into history, screenshots and shared links.
 */
export default function ScalperPopout() {
  const [apiKey, setApiKey] = useState<string | null>(null)
  const [wsUrl, setWsUrl] = useState<string | null>(null)
  const [noKey, setNoKey] = useState(false)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [keyRes, cfgRes] = await Promise.all([
          fetch('/api/websocket/apikey').then((r) => r.json()),
          fetch('/api/websocket/config').then((r) => r.json()),
        ])
        if (!alive) return
        if (keyRes.status !== 'success') {
          setNoKey(true)
          return
        }
        setApiKey(keyRes.api_key)
        setWsUrl(cfgRes.websocket_url || 'ws://127.0.0.1:8765')
      } catch {
        if (alive) setNoKey(true)
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  if (noKey) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
        <p className="text-sm text-muted-foreground">No API key found for the scalper terminal.</p>
        <a href="/apikey" className="text-sm font-medium text-primary underline">
          Generate an API key
        </a>
      </div>
    )
  }

  if (!apiKey || !wsUrl) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Loading scalper terminal…
      </div>
    )
  }

  return (
    <div className="h-screen overflow-hidden bg-background">
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Loading scalper terminal…
          </div>
        }
      >
        <ScalperTerminal
          apiKey={apiKey}
          wsUrl={wsUrl}
          // One-Click off in the popout until the user arms it there: arming is
          // a deliberate act per surface, not something to inherit silently.
          armed={false}
          // The popout window has nothing to float over — fill it.
          defaultMaximized
          onClose={() => {
            // The terminal's ✕ closes the window it lives in.
            window.close()
          }}
        />
      </Suspense>
    </div>
  )
}
