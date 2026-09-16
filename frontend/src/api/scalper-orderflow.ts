/**
 * Scalper Advisor + Orderflow Table plugin API.
 *
 * Served by blueprints/scalper_orderflow.py under the root path (not /api/v1),
 * so this uses webClient, which carries the session cookie and the CSRF token
 * — exactly like the TradingView watchlist client beside it.
 */

import { webClient } from './client'

export interface ScalperBasisLine {
  name?: string
  note?: string
}

export interface ScalperReversalRisk {
  score: number
  label: string
  action: string
  factors: Array<{ name: string; w: number; note: string }>
  ts: string
}

export interface ScalperAdvice {
  key: string
  name: string
  market: string
  lot: number
  status: 'LIVE' | 'CLOSED' | 'NO_DATA'
  signal: 'BUY CE' | 'BUY PE' | 'WAIT'
  side: 'CE' | 'PE' | null
  confidence: number
  fut_symbol: string
  spot: number | null
  strike: number | null
  option_symbol: string | null
  entry_premium: number | null
  current_premium: number | null
  target_premium: number | null
  sl_premium: number | null
  target_spot: number | null
  sl_spot: number | null
  rr: number | null
  trigger_time: string | null
  expiry: string | null
  dte: number | null
  pcr: number | null
  max_pain: number | null
  iv: number | null
  theta: number | null
  momentum: { trend: string; chp_30m?: number | null; day_chp?: number | null; src?: string }
  basis: string[]
  note: string
  pretrade?: { verdict: string; notes: string[]; risk_score: number } | null
  armed?: boolean
  armed_pnl_pct?: number
  armed_target_premium?: number | null
  armed_sl_premium?: number | null
  armed_target_pct?: number | null
  armed_sl_pct?: number | null
  armed_at?: string | null
  reversal_risk?: ScalperReversalRisk | null
  revision_log?: Array<{ ts: string; msg: string }>
}

export interface ScalperAlert {
  id: string
  key: string
  name?: string
  market?: string
  side: 'CE' | 'PE'
  strike: number | null
  option_symbol: string
  entry_premium: number
  current_premium: number
  target_premium: number | null
  sl_premium: number | null
  pnl_pct?: number
  status: 'ACTIVE' | 'CLOSED'
  created_at: string
  closed_at?: string | null
  close_reason?: string | null
  close_pnl_pct?: number
  close_outcome?: 'WIN' | 'LOSS'
  basis?: string[]
  armed?: boolean
  reversal_risk?: ScalperReversalRisk | null
}

export interface ScalperMonitorEvent {
  ts: string
  key: string
  severity: 'INFO' | 'WARN' | 'DANGER' | 'SUCCESS'
  msg: string
}

export interface ScalperAdvisorResponse {
  generated_at: string
  instruments: ScalperAdvice[]
  active_signals: number
  monitor: {
    armed: Record<string, unknown>
    events: ScalperMonitorEvent[]
    since: string | null
    alerts?: ScalperAlert[]
  }
  new_events: ScalperMonitorEvent[]
  error?: string
  disclaimer: string
}

export interface OrderflowRow {
  key: string
  name: string
  market: string
  ltp?: number | null
  chp?: number | null
  delta_bias?: string
  session_delta?: number
  session_cvd?: number
  total_volume?: number
  poc?: number
  vah?: number
  val?: number
  bar_count?: number
  target_symbol?: string
  error?: string
}

export interface OrderflowBar {
  timestamp: number
  time: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  buy_vol: number
  sell_vol: number
  buy_pct: number
  sell_pct: number
  delta: number
  delta_pct: number
  imbalance_type: string
  imbalance_ratio: number
  imbalance_label: string
  is_stacked: boolean
  cvd: number
}

export interface OrderflowDetail {
  symbol: string
  target_symbol: string
  root: string
  name: string
  timeframe: string
  bars: OrderflowBar[]
  summary: {
    ltp: number
    ch: number
    chp: number
    total_volume: number
    total_buy_vol: number
    total_sell_vol: number
    session_delta: number
    session_cvd: number
    delta_bias: string
    imbalance_summary: string
    poc: number
    vah: number
    val: number
    bar_count: number
    updated_at: number
  }
}

export const scalperApi = {
  getAdvisor: async (
    refresh = false,
    action?: { arm?: string; disarm?: string; armAlertId?: string }
  ): Promise<ScalperAdvisorResponse> => {
    const params: Record<string, string> = {}
    if (refresh) params.refresh = '1'
    if (action?.arm) params.arm = action.arm
    if (action?.disarm) params.disarm = action.disarm
    if (action?.armAlertId) params.arm_alert_id = action.armAlertId
    const response = await webClient.get('/plugins/scalper/advisor', { params })
    return response.data
  },

  closeAlert: async (alertId: string, reason = 'Manual close'): Promise<void> => {
    await webClient.post('/plugins/scalper/close', { alert_id: alertId, reason })
  },

  getAlertChart: async (
    alertId: string,
    symbol: string,
    tf = '5m'
  ): Promise<{ alert: ScalperAlert; timeframe: string; candles: number[][] }> => {
    const response = await webClient.get('/plugins/scalper/chart', {
      params: { alert_id: alertId, symbol, tf },
    })
    return response.data
  },
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

  getHealth: async (): Promise<{ status: string; scalper_db: boolean; orderflow_db: boolean }> => {
    const response = await webClient.get('/plugins/orderflow/health')
    return response.data
  },
}
