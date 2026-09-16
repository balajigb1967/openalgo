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
