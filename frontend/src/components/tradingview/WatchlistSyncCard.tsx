/**
 * Watchlist Sync card for the TradingView page.
 *
 * One card, four concerns: the webhook URL TradingView alerts POST to, the
 * target watchlist config, a paste-import box for one-off bulk adds, and the
 * recent-sync log. Talks to blueprints/tv_watchlist.py via
 * frontend/src/api/tv-watchlist.ts.
 */

import { Copy, ListPlus, RefreshCw, RotateCcw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { tvWatchlistApi } from '@/api/tv-watchlist'
import type { TVLogEntry, TVSyncResult, TVWatchlistConfig, WatchlistOption } from '@/api/tv-watchlist'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { showToast } from '@/utils/toast'

const OUTCOME_LABEL: Record<TVLogEntry['outcome'], string> = {
  added: 'Added',
  skipped: 'Skipped',
  failed: 'Not found',
}

function outcomeBadgeVariant(outcome: TVLogEntry['outcome']): 'default' | 'secondary' | 'destructive' {
  if (outcome === 'added') return 'default'
  if (outcome === 'failed') return 'destructive'
  return 'secondary'
}

export function WatchlistSyncCard() {
  const [config, setConfig] = useState<TVWatchlistConfig | null>(null)
  const [watchlists, setWatchlists] = useState<WatchlistOption[]>([])
  const [log, setLog] = useState<TVLogEntry[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Import form
  const [importText, setImportText] = useState('')
  const [importListId, setImportListId] = useState<string>('')
  const [importResult, setImportResult] = useState<TVSyncResult | null>(null)
  const [importing, setImporting] = useState(false)

  const loadAll = useCallback(async () => {
    try {
      const [{ config: cfg, watchlists: wls }, entries] = await Promise.all([
        tvWatchlistApi.getConfig(),
        tvWatchlistApi.log(),
      ])
      setConfig(cfg)
      setWatchlists(wls)
      setImportListId((prev) => (prev ? prev : cfg.chart_watchlist_id ? String(cfg.chart_watchlist_id) : ''))
      setLog(entries)
      setLoadError(null)
    } catch {
      setLoadError('Could not load the watchlist sync settings')
    }
  }, [])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  const webhookPath = config ? `/tvwatchlist/webhook/${config.webhook_token}` : ''
  const webhookUrl = typeof window !== 'undefined' ? `${window.location.origin}${webhookPath}` : webhookPath

  const save = async (patch: Partial<TVWatchlistConfig>) => {
    setSaving(true)
    try {
      const updated = await tvWatchlistApi.saveConfig(patch)
      setConfig(updated)
      showToast.success('Watchlist sync settings saved')
    } catch {
      showToast.error('Could not save the settings')
    } finally {
      setSaving(false)
    }
  }

  const rotate = async () => {
    try {
      const token = await tvWatchlistApi.rotateToken()
      setConfig((prev) => (prev ? { ...prev, webhook_token: token } : prev))
      showToast.success('New webhook link generated. Update your TradingView alerts.')
    } catch {
      showToast.error('Could not rotate the webhook link')
    }
  }

  const runImport = async () => {
    if (!importText.trim()) {
      showToast.error('Paste at least one symbol')
      return
    }
    setImporting(true)
    setImportResult(null)
    try {
      const result = await tvWatchlistApi.import(
        importText,
        importListId ? Number(importListId) : undefined,
        undefined
      )
      setImportResult(result)
      const entries = await tvWatchlistApi.log()
      setLog(entries)
      if (result.status === 'success') {
        showToast.success(result.message)
      } else {
        showToast.error(result.message)
      }
    } catch {
      showToast.error('Import failed')
    } finally {
      setImporting(false)
    }
  }

  if (loadError) {
    return (
      <Card className="mb-8">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">{loadError}</CardContent>
      </Card>
    )
  }

  if (!config) {
    return (
      <Card className="mb-8">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">Loading…</CardContent>
      </Card>
    )
  }

  return (
    <Card className="mb-8">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-primary">Watchlist Sync</CardTitle>
            <CardDescription>
              Add symbols from TradingView alerts straight into your watchlists
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Sync enabled</span>
            <Switch
              checked={config.enabled}
              disabled={saving}
              onCheckedChange={(checked) => save({ enabled: checked })}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Webhook URL */}
        <div className="space-y-2">
          <Label>Webhook URL for alerts</Label>
          <div className="flex items-center gap-2 p-3 bg-muted rounded-lg">
            <code className="flex-1 text-sm font-mono truncate" title={webhookUrl}>
              {webhookUrl}
            </code>
            <Button variant="secondary" size="sm" onClick={() => navigator.clipboard.writeText(webhookUrl).then(() => showToast.success('Webhook URL copied to clipboard', 'clipboard'))}>
              <Copy className="h-4 w-4 mr-1" />
              Copy
            </Button>
            <Button variant="ghost" size="sm" onClick={rotate} title="Replace this link. Existing alerts must be updated.">
              <RotateCcw className="h-4 w-4 mr-1" />
              New link
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            In TradingView, create an alert and set the message to:{' '}
            <code className="font-mono">{'{"ticker": "{{ticker}}", "exchange": "{{exchange}}"}'}</code>{' '}
            — every symbol the alert fires for lands in your watchlist.
          </p>
        </div>

        {/* Target config */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label>Charting watchlist</Label>
            <Select
              value={config.chart_watchlist_id ? String(config.chart_watchlist_id) : ''}
              onValueChange={(value) => save({ chart_watchlist_id: Number(value) })}
            >
              <SelectTrigger>
                <SelectValue placeholder="Choose a watchlist" />
              </SelectTrigger>
              <SelectContent>
                {watchlists.map((wl) => (
                  <SelectItem key={wl.id} value={String(wl.id)}>
                    {wl.name} ({wl.count})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-end gap-2 pb-1">
            <Switch
              id="tv-include-historify"
              checked={config.include_historify}
              disabled={saving}
              onCheckedChange={(checked) => save({ include_historify: checked })}
            />
            <Label htmlFor="tv-include-historify">Also add to the Historify watchlist</Label>
          </div>
        </div>

        {/* Paste import */}
        <div className="space-y-2 border-t pt-4">
          <div className="flex items-center justify-between">
            <Label className="flex items-center gap-2">
              <ListPlus className="h-4 w-4" />
              Import symbols now
            </Label>
            <span className="text-xs text-muted-foreground">NSE:RELIANCE, BSE:SENSEX, MCX:CRUDEOIL…</span>
          </div>
          <Textarea
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            placeholder={'NSE:RELIANCE\nBSE:SENSEX\nMCX:CRUDEOIL\nRELIANCE'}
            rows={4}
            className="font-mono text-sm"
          />
          <div className="flex items-center gap-2">
            <Select value={importListId} onValueChange={setImportListId}>
              <SelectTrigger className="w-56">
                <SelectValue placeholder="Same as webhook" />
              </SelectTrigger>
              <SelectContent>
                {watchlists.map((wl) => (
                  <SelectItem key={wl.id} value={String(wl.id)}>
                    {wl.name} ({wl.count})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button onClick={runImport} disabled={importing || !importText.trim()}>
              {importing ? (
                <>
                  <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                  Importing…
                </>
              ) : (
                'Import'
              )}
            </Button>
          </div>

          {importResult && (
            <div className="rounded-lg border p-3 space-y-2">
              <p className="text-sm font-medium">{importResult.message}</p>
              {importResult.unresolved.length > 0 && (
                <Alert>
                  <AlertDescription>
                    <span className="font-medium">Not found in the symbol master: </span>
                    {importResult.unresolved.map((u) => u.tv_symbol).join(', ')}
                  </AlertDescription>
                </Alert>
              )}
              {importResult.results.length > 0 && (
                <div className="max-h-40 overflow-y-auto space-y-1">
                  {importResult.results.map((r, i) => (
                    <div key={`${r.exchange}-${r.symbol}-${i}`} className="flex items-center gap-2 text-sm">
                      <Badge variant={outcomeBadgeVariant(r.outcome)}>{r.outcome}</Badge>
                      <span className="font-mono">
                        {r.exchange}:{r.symbol}
                      </span>
                      {r.message && <span className="text-muted-foreground">{r.message}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Recent activity */}
        {log.length > 0 && (
          <div className="space-y-2 border-t pt-4">
            <Label>Recent syncs</Label>
            <div className="max-h-48 overflow-y-auto space-y-1">
              {log.map((entry) => (
                <div key={entry.id} className="flex items-center gap-2 text-sm">
                  <Badge variant={outcomeBadgeVariant(entry.outcome)}>
                    {OUTCOME_LABEL[entry.outcome] ?? entry.outcome}
                  </Badge>
                  <span className="font-mono">{entry.tv_symbol || `${entry.exchange}:${entry.symbol}`}</span>
                  <span className="text-xs text-muted-foreground">
                    via {entry.source === 'webhook' ? 'alert' : 'import'}
                    {entry.created_at ? ` · ${new Date(entry.created_at).toLocaleString()}` : ''}
                  </span>
                  {entry.message && <span className="text-xs text-muted-foreground truncate">{entry.message}</span>}
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
