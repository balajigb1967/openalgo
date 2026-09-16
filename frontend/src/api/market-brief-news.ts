/**
 * Market Brief + News plugin API.
 *
 * Served by blueprints/scalper_orderflow.py under the root path (not /api/v1),
 * so this uses webClient, which carries the session cookie and the CSRF token.
 */

import { webClient } from './client'
import type { OrderflowBar, OrderflowDetail, OrderflowRow } from './market-brief-news-types'

export type { OrderflowBar, OrderflowDetail, OrderflowRow }

export interface BriefQuote {
  label: string
  symbol: string
  name?: string
  ltp: number | null
  ch: number | null
  chp: number | null
}

export interface BriefOptions {
  spot?: number | null
  atm?: number | null
  pcr?: number | null
  max_pain?: number | null
  atm_iv?: number | null
  call_wall?: number | null
  put_wall?: number | null
  likely_direction?: string
  likely_confidence?: number | null
  likely_range?: string
}

export interface BriefNewsItem {
  title: string
  link: string
  source: string
  summary?: string
}

export interface BriefEvent {
  title: string
  date: string
  impact: string
  actual?: string
  forecast?: string
  previous?: string
  currency?: string
}

export interface BriefDriver {
  topic: string
  detail: string
  bias: string
}

export interface BriefSector {
  sector: string
  ltp?: number | null
  chp: number
  score: number
  verdict: string
}

export interface MarketBriefResponse {
  generated_at: string
  indices: BriefQuote[]
  commodities: BriefQuote[]
  options: BriefOptions
  news: BriefNewsItem[]
  events: BriefEvent[]
  geopolitical: {
    rates?: Record<string, { label: string; ltp: number | null; chp: number | null }>
    drivers: BriefDriver[]
    headlines: Array<{ title: string; link: string; source: string; category: string; impact: string }>
    sectors: BriefSector[]
  }
  overnight_cues: {
    sentiment: string
    summary: string
    global_indices: BriefQuote[]
    macro_indicators: BriefQuote[]
    institutional_flow: { fii_net?: string; dii_net?: string; date?: string; net_bias?: string }
  }
  gameplan: {
    nifty?: GameplanLeg
    banknifty?: GameplanLeg
    likely_direction?: string
    likely_range?: string
    scalper_rules: string[]
  }
  session_stance: {
    phase: string
    exchange: string
    phase_label: string
    stance: string
    icon: string
    nifty_ltp: number | null
    nifty_chp: number | null
    pcr: number | null
    max_pain: number | null
    narrative: string
    updated_at: string
    is_live: boolean
  }
  summary_markdown: string
}

export interface GameplanLeg {
  name: string
  ltp: number
  chp: number
  s1: number
  s2: number
  pivot: number
  r1: number
  r2: number
  pcr?: number | null
  max_pain?: number | null
  bias: string
  action: string
  scalp_trigger: string
}

export interface NewsItem {
  id?: string
  title: string
  source: string
  provider_key?: string
  category?: string
  published: number
  link: string
  summary?: string
  urgency?: number
  symbols?: string[]
  sentiment?: string
}

export interface NewsResponse {
  source?: string
  articles?: NewsItem[]
  symbol?: string
  count?: number
  sources?: Array<{ name: string; count: number }>
  items?: NewsItem[]
}

export const orderflowApi = {
  getTable: async (tf = '5m'): Promise<{ status: string; timeframe: string; rows: OrderflowRow[] }> => {
    const response = await webClient.get('/plugins/orderflow/table', { params: { tf } })
    return response.data
  },

  getTableRefresh: async (
    tf = '5m',
    refresh = false
  ): Promise<{ status: string; timeframe: string; rows: OrderflowRow[] }> => {
    const response = await webClient.get('/plugins/orderflow/table', {
      params: refresh ? { tf, refresh: '1' } : { tf },
    })
    return response.data
  },

  getDetail: async (symbol: string, tf = '5m', bars = 25, refresh = false): Promise<OrderflowDetail> => {
    const response = await webClient.get('/plugins/orderflow/detail', {
      params: refresh ? { symbol, tf, bars, refresh: '1' } : { symbol, tf, bars },
    })
    return response.data
  },
}

export const briefApi = {
  getBrief: async (refresh = false): Promise<MarketBriefResponse> => {
    const response = await webClient.get('/plugins/brief', {
      params: refresh ? { refresh: '1' } : undefined,
    })
    return response.data
  },
}

export const newsApi = {
  getNews: async (limit = 60, refresh = false): Promise<NewsResponse> => {
    const response = await webClient.get('/plugins/news', {
      params: refresh ? { limit, refresh: '1' } : { limit },
    })
    return response.data
  },

  getSymbolNews: async (symbol: string, limit = 40): Promise<NewsResponse> => {
    const response = await webClient.get('/plugins/news/symbol', { params: { symbol, limit } })
    return response.data
  },
}
