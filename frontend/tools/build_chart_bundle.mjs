// Bundles openalgo-charts (core + indicators + transform tiers) into one
// IIFE file exposing window.OpenAlgoCharts for the mobile chart WebView.
// Run: node tools/build_chart_bundle.mjs   (from openalgo/frontend/)
import { rolldown } from 'rolldown'
import { mkdirSync, writeFileSync, appendFileSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const here = path.dirname(fileURLToPath(import.meta.url))
const outDir = path.join(here, '..', '..', '..', 'openalgo-mobile-fno', 'assets', 'charts')
mkdirSync(outDir, { recursive: true })

const bundle = await rolldown({
  input: path.join(here, 'oa_chart_entry.mjs'),
  platform: 'browser',
})
const { output } = await bundle.generate({
  format: 'iife',
  name: 'OpenAlgoCharts',
})
// IIFE output is a bare `var OpenAlgoCharts = (...)` — attach it to window
// explicitly so the WebView always finds it regardless of scope handling.
const code = `${output[0].code}\n;window.OpenAlgoCharts = typeof OpenAlgoCharts !== 'undefined' ? OpenAlgoCharts : window.OpenAlgoCharts;\n`
const outfile = path.join(outDir, 'openalgo-charts.bundle.js')
writeFileSync(outfile, code)
console.log(`bundle written: ${outfile} (${(code.length / 1024).toFixed(0)} KB)`)
