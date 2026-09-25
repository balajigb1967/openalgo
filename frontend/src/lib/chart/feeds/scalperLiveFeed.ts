/**
 * A live chart feed for the scalper cells, over the scalping backend.
 *
 * History comes from `/scalping/api/history` (the same route ScalpChart used,
 * with its fixed lookbacks: 1m=1d, 5m=3d, 15m=9d) and the forming bar is
 * driven by the shared MarketDataManager Quote stream, so N cells on one
 * instrument share one websocket subscription with the depth tables beside
 * them rather than fanning out per chart.
 *
 * The one convention this feed must honour is the backend's IST-shifted bar
 * times. `/scalping/api/history` bakes UTC+5:30 into every `time` it returns,
 * so live ticks are shifted by the same IST_OFFSET before bucketing — the
 * chart then reads in IST session time (10:00..15:00) exactly like the old
 * ScalpChart did, and history and live bars land on the same axis.
 *
 * The feed is deliberately symbol-blind: `symbol` and `exchange` arrive on
 * every request, so one instance serves every cell and every instrument, and
 * a chart the user has added indicators or a chart type to can follow a
 * strike change without being rebuilt.
 */

import type { Bar, BarsRequest, DataFeed } from 'openalgo-charts'
import { MarketDataManager } from '@/lib/MarketDataManager'
import { scalpingApi } from '@/api/scalping'

/** The IST shift the backend bakes into bar times (5h30m = 19800s). */
export const IST_OFFSET = 19800

/** Seconds per bar for the intervals the scalper offers. */
const INTERVAL_SEC: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  D: 86400,
}

/** Bucket seconds for a code, defaulting to one minute. */
export function intervalSec(interval: string): number {
  return INTERVAL_SEC[interval] ?? 60
}

/**
 * The bucket a tick belongs to, in the backend's IST-shifted seconds.
 *
 * `timestamp` is the broker's ISO stamp when it has one; wall clock otherwise.
 * Exported for its test — the offset is the whole agreement between history
 * and live bars, and dropping it silently misaligns every live candle by 5.5h.
 */
export function istBucket(ts: string | undefined, interval: string, nowMs = Date.now()): number {
  const parsed = ts ? Date.parse(ts) : Number.NaN
  const epochUtc = Number.isNaN(parsed) ? Math.floor(nowMs / 1000) : Math.floor(parsed / 1000)
  const sec = intervalSec(interval)
  return Math.floor((epochUtc + IST_OFFSET) / sec) * sec
}

/** A wire candle to an engine bar, or null when it cannot be trusted. */
export function candleToBar(c: {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}): Bar | null {
  const time = Math.round(Number(c?.time))
  const close = Number(c?.close)
  if (!Number.isFinite(time) || !Number.isFinite(close)) return null
  const open = Number(c.open)
  const high = Number(c.high)
  const low = Number(c.low)
  const volume = Number(c.volume)
  return {
    time,
    open: Number.isFinite(open) ? open : close,
    high: Number.isFinite(high) ? high : close,
    low: Number.isFinite(low) ? low : close,
    close,
    ...(Number.isFinite(volume) ? { volume: Math.round(volume) } : {}),
  }
}

/**
 * The forming bar for one instrument, updated in place from Quote ticks.
 *
 * Day volume arrives cumulative, so the bar's volume is the delta since the
 * bucket opened (`barStartVol`), and an absent stream volume leaves the bar's
 * at whatever it was. Exported pure so the tick arithmetic is testable without
 * a websocket.
 */
export interface FormingBar {
  bucket: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  barStartVol: number | null
}

export function applyTick(
  bar: FormingBar | undefined,
  bucket: number,
  ltp: number,
  dayVolume: number | undefined
): { bar: FormingBar; out: Bar; isNew: boolean } {
  if (!bar || bucket > bar.bucket) {
    const fresh: FormingBar = {
      bucket,
      open: ltp,
      high: ltp,
      low: ltp,
      close: ltp,
      volume: 0,
      barStartVol: typeof dayVolume === 'number' ? dayVolume : null,
    }
    return {
      bar: fresh,
      out: { time: bucket, open: ltp, high: ltp, low: ltp, close: ltp, volume: 0 },
      isNew: true,
    }
  }
  // A stale tick older than the current bucket says nothing about it.
  if (bucket < bar.bucket) return { bar, out: formingOut(bar), isNew: false }
  bar.high = Math.max(bar.high, ltp)
  bar.low = Math.min(bar.low, ltp)
  bar.close = ltp
  if (typeof dayVolume === 'number' && bar.barStartVol != null) {
    bar.volume = Math.max(0, Math.round(dayVolume - bar.barStartVol))
  }
  return { bar, out: formingOut(bar), isNew: false }
}

function formingOut(bar: FormingBar): Bar {
  return {
    time: bar.bucket,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
  }
}

/**
 * Build the feed. One instance is meant to be shared by every scalper chart
 * on the page; the module exports `scalperLiveFeed` for exactly that.
 */
export function createScalperLiveFeed(): DataFeed {
  /** Forming bar per instrument, seeded from history so the live stream
   *  continues the last closed candle instead of opening a rival one. */
  const forming = new Map<string, FormingBar>()
  const keyOf = (req: BarsRequest) => `${req.exchange}:${req.symbol}`

  return {
    async getBars(req: BarsRequest): Promise<Bar[]> {
      if (!req.symbol || !req.exchange) return []
      try {
        const d = await scalpingApi.getHistory(req.symbol, req.exchange, req.interval)
        if (d.status !== 'success') return []
        const bars: Bar[] = []
        for (const c of d.candles ?? []) {
          const bar = candleToBar(c)
          if (bar) bars.push(bar)
        }
        bars.sort((a, b) => a.time - b.time)
        let last = bars[bars.length - 1]
        // Several brokers serve no history for thinly-traded option contracts
        // even while quotes flow. Seed the forming bar from the REST quote
        // snapshot so the canvas paints one live candle immediately instead
        // of an empty "no bars" panel waiting for the next bucket.
        if (!last && d.last_quote && d.last_quote.ltp > 0) {
          const bucket = istBucket(undefined, req.interval)
          const seeded = applyTick(undefined, bucket, d.last_quote.ltp, undefined)
          forming.set(keyOf(req), seeded.bar)
          return [seeded.out]
        }
        if (last) {
          forming.set(keyOf(req), {
            bucket: last.time,
            open: last.open,
            high: last.high,
            low: last.low,
            close: last.close,
            volume: last.volume ?? 0,
            // Unknown until the next tick with a day volume on it.
            barStartVol: null,
          })
        }
        return bars
      } catch (error) {
        if (error instanceof Error && error.name === 'AbortError') throw error
        // Everything else is an empty load: the engine draws its own
        // "no bars" state and a refresh-on-bar-close retries from the tail.
        return []
      }
    },

    subscribeBars(
      req: BarsRequest,
      onBar: (bar: Bar) => void
    ): () => void {
      const key = keyOf(req)
      const interval = req.interval
      return MarketDataManager.getInstance().subscribe(
        req.symbol,
        req.exchange,
        'Quote',
        (sd) => {
          const q = sd?.data
          const ltp = q?.ltp
          if (ltp == null || !Number.isFinite(ltp) || ltp <= 0) return
          const bucket = istBucket(q?.timestamp, interval)
          const dayVolume = typeof q?.volume === 'number' ? q.volume : undefined
          const { bar, out } = applyTick(forming.get(key), bucket, ltp, dayVolume)
          forming.set(key, bar)
          onBar(out)
        }
      )
    },
  }
}

/** The shared feed instance every scalper chart uses. */
export const scalperLiveFeed = createScalperLiveFeed()
