import { describe, expect, it } from 'vitest'

import { IST_OFFSET, applyTick, candleToBar, istBucket, intervalSec } from './scalperLiveFeed'

/** 2026-09-25T10:00:00 IST = 04:30 UTC. */
const UTC_0430 = Date.UTC(2026, 8, 25, 4, 30, 0)

describe('istBucket', () => {
  it('shifts epoch ticks into the backend IST-shifted seconds and buckets them', () => {
    const bucket = istBucket(undefined, '1m', UTC_0430)
    expect(bucket).toBe(Math.floor((UTC_0430 / 1000 + IST_OFFSET) / 60) * 60)
    expect(bucket % 60).toBe(0)
  })

  it('uses the broker timestamp when present', () => {
    const iso = new Date(UTC_0430).toISOString()
    expect(istBucket(iso, '5m', UTC_0430 + 1000)).toBe(
      Math.floor((UTC_0430 / 1000 + IST_OFFSET) / 300) * 300
    )
  })

  it('scales the bucket with the interval', () => {
    expect(intervalSec('1m')).toBe(60)
    expect(intervalSec('5m')).toBe(300)
    expect(intervalSec('15m')).toBe(900)
    expect(intervalSec('2m')).toBe(60) // unknown codes fall back to 1m
  })
})

describe('applyTick', () => {
  it('opens a new bar when the bucket moves forward', () => {
    const { bar, out, isNew } = applyTick(undefined, 1000, 119.25, 500)
    expect(isNew).toBe(true)
    expect(bar.barStartVol).toBe(500)
    expect(out).toEqual({ time: 1000, open: 119.25, high: 119.25, low: 119.25, close: 119.25, volume: 0 })
  })

  it('extends the bar high/low/close and derives bucket volume from the day delta', () => {
    const first = applyTick(undefined, 1000, 119, 1000)
    const second = applyTick(first.bar, 1000, 121, 1250)
    expect(second.isNew).toBe(false)
    expect(second.out.high).toBe(121)
    expect(second.out.low).toBe(119)
    expect(second.out.close).toBe(121)
    expect(second.out.volume).toBe(250)
  })

  it('ignores a stale tick older than the current bucket', () => {
    const first = applyTick(undefined, 1000, 119, 1000)
    const stale = applyTick(first.bar, 900, 1, 0)
    expect(stale.out.close).toBe(119)
  })

  it('keeps barStartVol null until a day volume arrives, leaving volume at zero', () => {
    const first = applyTick(undefined, 1000, 119, undefined)
    expect(first.bar.barStartVol).toBeNull()
    const second = applyTick(first.bar, 1000, 120, undefined)
    expect(second.out.volume).toBe(0)
    // A later tick carrying day volume starts measuring from itself rather
    // than inventing a baseline.
    expect(applyTick(second.bar, 1000, 121, 400).out.volume).toBe(0)
  })
})

describe('candleToBar', () => {
  it('maps a wire candle, rounding volume and tolerating missing fields', () => {
    expect(candleToBar({ time: 1000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10.6 })).toEqual({
      time: 1000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 11,
    })
    expect(candleToBar({ time: 1000, open: NaN, high: 2, low: 0.5, close: 1.5, volume: 0 })).toEqual({
      time: 1000, open: 1.5, high: 2, low: 0.5, close: 1.5, volume: 0,
    })
    expect(candleToBar({ time: NaN, open: 1, high: 1, low: 1, close: 1, volume: 0 })).toBeNull()
  })
})
