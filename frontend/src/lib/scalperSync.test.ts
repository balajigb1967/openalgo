import { describe, expect, it } from 'vitest'

import { parseSyncSymbol, rootOf } from './scalperSync'

describe('rootOf', () => {
  it('keeps the MCX short forms intact (longest-prefix wins)', () => {
    expect(rootOf('CRUDEOILM')).toBe('CRUDEOILM')
    expect(rootOf('CRUDEOILMINI')).toBe('CRUDEOILMINI')
    expect(rootOf('GOLDM')).toBe('GOLDM')
    expect(rootOf('SILVERM')).toBe('SILVERM')
    expect(rootOf('SILVERMIC')).toBe('SILVERMIC')
    expect(rootOf('CRUDEOIL')).toBe('CRUDEOIL')
  })

  it('peels weekly expiry tokens off index roots', () => {
    expect(rootOf('NIFTY29SEP26')).toBe('NIFTY')
    expect(rootOf('BANKNIFTY30OCT26FUT')).toBe('BANKNIFTY')
  })
})

describe('parseSyncSymbol', () => {
  it('splits an index weekly option into root, strike and side', () => {
    const p = parseSyncSymbol('NFO:NIFTY29SEP2623150CE')
    expect(p.root).toBe('NIFTY')
    expect(p.strike).toBe(23150)
    expect(p.optionType).toBe('CE')
    expect(p.optionSymbol).toBe('NIFTY29SEP2623150CE')
  })

  it('splits an MCX mini option and keeps the short root', () => {
    const p = parseSyncSymbol('MCX:CRUDEOILM19OCT266300PE')
    expect(p.root).toBe('CRUDEOILM')
    expect(p.strike).toBe(6300)
    expect(p.optionType).toBe('PE')
  })

  it('handles fractional strikes', () => {
    const p = parseSyncSymbol('MCX:GOLDM18DEC267250.5CE')
    expect(p.root).toBe('GOLDM')
    expect(p.strike).toBe(7250.5)
  })

  it('parses a non-option symbol to its family root', () => {
    expect(parseSyncSymbol('MCX:CRUDEOILM').root).toBe('CRUDEOILM')
    expect(parseSyncSymbol('NSE:RELIANCE').root).toBe('RELIANCE')
    expect(parseSyncSymbol('NSE_INDEX:NIFTY').root).toBe('NIFTY')
  })

  it('leaves garbage without a root rather than guessing', () => {
    expect(parseSyncSymbol('GLOBAL:DOLLAR').root).toBe('DOLLAR')
    expect(parseSyncSymbol('X:').root).toBe('')
  })
})
