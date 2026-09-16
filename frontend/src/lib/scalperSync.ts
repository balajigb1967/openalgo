/**
 * Chart ⇄ Scalper sync bus.
 *
 * The Trading page publishes the focused pane's symbol and the active advisor
 * alert here; the embedded ScalperTerminal subscribes and follows. Kept as a
 * tiny module-level store (not React context) so the pieces can live pages
 * apart — the advisor panel is a sidebar component, the terminal is embedded
 * beside the chart grid, and neither should have to know the other exists.
 *
 * Parse mirrors OpenAlgo's `EXCHANGE:SYMBOL` convention used by the watchlist,
 * option chain and depth panels. MCX roots are the instrument *family* names
 * the scalper advisor already speaks (CRUDEOIL, GOLD, NATURALGAS…).
 */

export interface ScalperSyncState {
  /** Raw focused symbol, as `EXCHANGE:SYMBOL` (e.g. `MCX:CRUDEOIL`). */
  symbol: string
  /** Option contract on the focused pane (…CE/…PE suffix), else ''. */
  optionSymbol: string
  /** Underlying family root (CRUDEOIL, NIFTY, RELIANCE…), else ''. */
  root: string
  /** `CE` / `PE` when the focused symbol is an option contract. */
  optionType: 'CE' | 'PE' | null
  /** Focused option's strike (parsed from the symbol), else null. */
  strike: number | null
}

type Listener = (s: ScalperSyncState) => void

let state: ScalperSyncState = {
  symbol: '',
  optionSymbol: '',
  root: '',
  optionType: null,
  strike: null,
}

const listeners = new Set<Listener>()

/** Index/stock roots that trade weekly options on the F&O exchanges. */
const MCX_ROOTS = [
  'CRUDEOIL',
  'CRUDEOILMINI',
  'GOLD',
  'GOLDM',
  'GOLDGUINEA',
  'SILVER',
  'SILVERM',
  'SILVERMIC',
  'COPPER',
  'ZINC',
  'LEAD',
  'NICKEL',
  'ALUMINIUM',
  'NATURALGAS',
  'NATURALGASMINI',
  'MENTHAOIL',
  'COTTONCANDY',
]

export function rootOf(symbol: string): string {
  const s = symbol.toUpperCase()
  // Known MCX family names first — CRUDEOILMINI must not shrink to CRUDEOIL.
  const mcx = MCX_ROOTS.find((r) => s.startsWith(r))
  if (mcx) return mcx
  // Weekly options append date/offset tokens to the root; peel the common ones.
  return s.replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}.*$/, '')
}

export function parseSyncSymbol(symbol: string): ScalperSyncState {
  const sym = symbol.split(':')[1] ?? symbol
  const m = sym.match(/^(.+?)(\d+(?:\.\d+)?)(CE|PE)$/)
  if (m) {
    return {
      symbol,
      optionSymbol: sym,
      root: m[1],
      optionType: m[3] as 'CE' | 'PE',
      strike: Number(m[2]),
    }
  }
  return { symbol, optionSymbol: '', root: sym ? rootOf(sym) : '', optionType: null, strike: null }
}

/** Publish the focused pane's symbol (empty string clears the link). */
export function setSyncSymbol(symbol: string): void {
  const next = parseSyncSymbol(symbol)
  if (
    next.symbol === state.symbol &&
    next.optionSymbol === state.optionSymbol &&
    next.root === state.root
  ) {
    return
  }
  state = next
  for (const l of listeners) l(state)
}

export function getSyncState(): ScalperSyncState {
  return state
}

export function subscribeSync(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export interface ScalperTarget {
  /** Advisor instrument key, e.g. CRUDEOIL — what the terminal's underlying maps to. */
  key: string
  /** Futures/underlying contract symbol for expiry resolution (NATURALGAS26SEP Fut style is NOT required; raw root suffices). */
  underlying: string
  exchange: string
  side: 'CE' | 'PE'
  strike: number
  source: 'alert' | 'chart'
}

type TargetListener = (t: ScalperTarget | null) => void

let target: ScalperTarget | null = null
const targetListeners = new Set<TargetListener>()

/**
 * Publish an advisor alert as the terminal's trade target. The terminal
 * switches exchange/underlying/segment/strikes to match it.
 */
export function setSyncTarget(t: ScalperTarget | null): void {
  const same =
    (!t && !target) ||
    (t &&
      target &&
      t.key === target.key &&
      t.side === target.side &&
      t.strike === target.strike &&
      t.exchange === target.exchange)
  if (same) return
  target = t
  for (const l of targetListeners) l(target)
}

export function getSyncTarget(): ScalperTarget | null {
  return target
}

export function subscribeSyncTarget(listener: TargetListener): () => void {
  targetListeners.add(listener)
  return () => {
    targetListeners.delete(listener)
  }
}
