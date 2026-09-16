/**
 * Orderflow table types, shared between the Orderflow panel and the
 * market-brief/news client module.
 */

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
