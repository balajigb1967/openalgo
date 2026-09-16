/**
 * TradingView Watchlist Sync API.
 *
 * Served by blueprints/tv_watchlist.py under the root path (not /api/v1), so
 * this uses webClient, which carries the session cookie and the CSRF token.
 * The config and the watchlists belong to the signed-in user; the webhook
 * itself authenticates with its URL token and is never called from here.
 */

import { webClient } from './client'

export interface TVWatchlistConfig {
  user_id: string
  enabled: boolean
  webhook_token: string
  webhook_secret: string
  chart_watchlist_id: number | null
  include_historify: boolean
}

export interface WatchlistOption {
  id: number
  name: string
  count: number
}

export interface TVSyncResultItem {
  symbol: string
  exchange: string
  tv_symbol: string
  outcome: 'added' | 'skipped' | 'failed'
  message: string | null
}

export interface TVSyncResult {
  status: 'success' | 'error'
  message: string
  added: number
  failed: number
  results: TVSyncResultItem[]
  unresolved: Array<{ tv_symbol: string; error: string }>
}

export interface TVLogEntry {
  id: number
  source: 'webhook' | 'import'
  outcome: 'added' | 'skipped' | 'failed'
  symbol: string
  exchange: string
  tv_symbol: string
  message: string | null
  created_at: string | null
}

interface Envelope<T> {
  status: 'success' | 'error'
  data: T
  message?: string
}

export const tvWatchlistApi = {
  getConfig: async (): Promise<{ config: TVWatchlistConfig; watchlists: WatchlistOption[] }> => {
    const res = await webClient.get<Envelope<{ config: TVWatchlistConfig; watchlists: WatchlistOption[] }>>(
      '/tvwatchlist/api/config'
    )
    return res.data.data
  },

  saveConfig: async (
    patch: Partial<Pick<TVWatchlistConfig, 'enabled' | 'webhook_secret' | 'chart_watchlist_id' | 'include_historify'>>
  ): Promise<TVWatchlistConfig> => {
    const res = await webClient.post<Envelope<TVWatchlistConfig>>('/tvwatchlist/api/config', patch)
    return res.data.data
  },

  rotateToken: async (): Promise<string> => {
    const res = await webClient.post<Envelope<{ webhook_token: string }>>('/tvwatchlist/api/token/rotate')
    return res.data.data.webhook_token
  },

  import: async (
    symbols: string,
    watchlistId?: number,
    includeHistorify?: boolean
  ): Promise<TVSyncResult> => {
    const res = await webClient.post<TVSyncResult & Envelope<unknown>>('/tvwatchlist/api/import', {
      symbols,
      ...(watchlistId !== undefined ? { watchlist_id: watchlistId } : {}),
      ...(includeHistorify !== undefined ? { include_historify: includeHistorify } : {}),
    })
    return res.data as TVSyncResult
  },

  log: async (): Promise<TVLogEntry[]> => {
    const res = await webClient.get<Envelope<TVLogEntry[]>>('/tvwatchlist/api/log')
    return res.data.data ?? []
  },
}
