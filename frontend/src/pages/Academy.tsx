import { GraduationCap, Loader2, RefreshCw, Send, WifiOff } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  askTutor,
  getCurriculum,
  getLesson,
  postQuiz,
  type EduGrade,
  type EduLesson,
  type EduLessonMeta,
} from '@/api/education'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/** Strip the lesson markdown bold markers — desktop renders plain text. */
const clean = (s: string) => s.replaceAll('**', '')

/** Axios errors carry their message on `response.data.message` or `message`. */
const errText = (e: unknown): string => {
  const anyErr = e as { message?: string; response?: { data?: { message?: string } } }
  return String(anyErr?.response?.data?.message ?? anyErr?.message ?? e)
}

const fmtNum = (v: unknown, dp = 2): string => {
  const d = typeof v === 'number' ? v : Number.parseFloat(String(v ?? ''))
  if (Number.isNaN(d)) return '—'
  return d.toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

const fmtOi = (v: unknown): string => {
  const d = typeof v === 'number' ? v : Number.parseFloat(String(v ?? ''))
  if (Number.isNaN(d)) return '—'
  const a = Math.abs(d)
  if (a >= 1e7) return `${(d / 1e7).toFixed(2)}Cr`
  if (a >= 1e5) return `${(d / 1e5).toFixed(1)}L`
  if (a >= 1e3) return `${(d / 1e3).toFixed(1)}K`
  return d.toFixed(0)
}

function QuoteCard({ data }: { data: EduLesson['data'] }) {
  const q = data.quote
  if (!q) return null
  const color = q.change_pct >= 0 ? 'text-profit' : 'text-loss'
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Quote
        </div>
        <div className="flex items-baseline gap-3">
          <span className={cn('text-2xl font-bold', color)}>{fmtNum(q.ltp)}</span>
          <span className={cn('text-sm font-semibold', color)}>
            {q.change_pct >= 0 ? '+' : ''}
            {fmtNum(q.change_pct)}%
          </span>
        </div>
        <div className="mt-3 grid grid-cols-4 gap-2 text-sm">
          {(
            [
              ['Open', q.open],
              ['High', q.high],
              ['Low', q.low],
              ['Prev close', q.prev_close],
            ] as const
          ).map(([k, v]) => (
            <div key={k}>
              <div className="text-xs text-muted-foreground">{k}</div>
              <div className="font-semibold">{fmtNum(v)}</div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

function ChainCard({ chain }: { chain: NonNullable<EduLesson['data']['chain']> }) {
  const cw = chain.call_wall as Record<string, unknown> | undefined
  const pw = chain.put_wall as Record<string, unknown> | undefined
  const row = (k: string, v: string) => (
    <div className="flex items-center justify-between py-0.5 text-sm">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-semibold">{v}</span>
    </div>
  )
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Option chain · {String(chain.expiry ?? '')}
        </div>
        {row(
          'ATM strike',
          `${String(chain.atm_strike ?? '—')} (call ${fmtNum(chain.atm_call_ltp)} / put ${fmtNum(chain.atm_put_ltp)})`
        )}
        {chain.pcr != null && row('PCR (OI)', String(chain.pcr))}
        {chain.max_pain != null && row('Max pain', String(chain.max_pain))}
        {cw && row('Call wall', `${String(cw.strike)} · OI ${fmtOi(cw.oi)} · ${String(cw.buildup ?? '')}`)}
        {pw && row('Put wall', `${String(pw.strike)} · OI ${fmtOi(pw.oi)} · ${String(pw.buildup ?? '')}`)}
      </CardContent>
    </Card>
  )
}

function IndicatorsCard({ ind }: { ind: NonNullable<EduLesson['data']['indicators']> }) {
  const macd = ind.macd as Record<string, unknown> | undefined
  const bb = ind.bollinger as Record<string, unknown> | undefined
  const row = (k: string, v: string) => (
    <div className="flex items-center justify-between py-0.5 text-sm">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-semibold">{v}</span>
    </div>
  )
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Daily indicators
        </div>
        {ind.rsi14 != null && row('RSI (14)', String(ind.rsi14))}
        {ind.sma20 != null && row('SMA 20', fmtNum(ind.sma20))}
        {ind.sma50 != null && row('SMA 50', fmtNum(ind.sma50))}
        {ind.ema20 != null && row('EMA 20', fmtNum(ind.ema20))}
        {macd &&
          row(
            'MACD (12,26,9)',
            `${String(macd.macd)} / sig ${String(macd.signal)} · ${String(macd.status ?? '')}`
          )}
        {bb && row('Bollinger 20,2σ', `${fmtNum(bb.upper)} / ${fmtNum(bb.middle)} / ${fmtNum(bb.lower)}`)}
      </CardContent>
    </Card>
  )
}

function CandlesCard({ candles }: { candles: NonNullable<EduLesson['data']['candles']> }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Recent daily candles
        </div>
        {candles.slice(0, 6).map((c, i) => {
          const up = Number(c.close) >= Number(c.open)
          return (
            <div key={i} className="flex items-center justify-between py-0.5 text-sm">
              <span className="w-14 text-muted-foreground">{String(c.date ?? '')}</span>
              <span className="text-muted-foreground">
                O {fmtNum(c.open)} · H {fmtNum(c.high)} · L {fmtNum(c.low)} ·{' '}
              </span>
              <span className={cn('font-semibold', up ? 'text-profit' : 'text-loss')}>
                C {fmtNum(c.close)}
              </span>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

export default function Academy() {
  const [lessons, setLessons] = useState<EduLessonMeta[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [selected, setSelected] = useState<EduLessonMeta | null>(null)
  const [lesson, setLesson] = useState<EduLesson | null>(null)
  const [lessonLoading, setLessonLoading] = useState(false)
  const [lessonError, setLessonError] = useState<string | null>(null)

  const [symbol, setSymbol] = useState('NIFTY')
  const [symbolDraft, setSymbolDraft] = useState('NIFTY')

  const [answers, setAnswers] = useState<Record<string, number>>({})
  const [grade, setGrade] = useState<EduGrade | null>(null)
  const [grading, setGrading] = useState(false)

  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)

  useEffect(() => {
    getCurriculum()
      .then((ls) => {
        setLessons(ls)
        setLoading(false)
      })
      .catch((e) => {
        setError(errText(e))
        setLoading(false)
      })
  }, [])

  const openLesson = useCallback(
    async (meta: EduLessonMeta) => {
      setSelected(meta)
      setLessonLoading(true)
      setLessonError(null)
      setLesson(null)
      setAnswers({})
      setGrade(null)
      setAnswer(null)
      try {
        const d = await getLesson(meta.id, symbol.trim().toUpperCase() || undefined)
        setLesson(d)
      } catch (e) {
        setLessonError(errText(e))
      } finally {
        setLessonLoading(false)
      }
    },
    [symbol]
  )

  const submitQuiz = useCallback(async () => {
    if (!selected || grading) return
    if (!lesson || Object.keys(answers).length < lesson.quiz.length) return
    setGrading(true)
    try {
      const g = await postQuiz(selected.id, answers)
      setGrade(g)
    } catch (e) {
      setError(errText(e))
    } finally {
      setGrading(false)
    }
  }, [answers, grading, lesson, selected])

  const sendQuestion = useCallback(async () => {
    const q = question.trim()
    if (!q || asking) return
    setAsking(true)
    setAnswer(null)
    try {
      const r = await askTutor({
        question: q,
        lesson: selected?.id,
        symbol: symbol.trim().toUpperCase() || undefined,
      })
      setAnswer(r.answer)
      setQuestion('')
    } catch (e) {
      setAnswer(`Tutor unavailable: ${errText(e)}`)
    } finally {
      setAsking(false)
    }
  }, [asking, question, selected, symbol])

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    )
  }

  if (error && !selected) {
    return (
      <div className="flex h-[60vh] flex-col items-center justify-center gap-3 text-center">
        <WifiOff className="h-8 w-8 text-muted-foreground" />
        <p>Academy unavailable: {error}</p>
        <Button variant="outline" onClick={() => window.location.reload()}>
          <RefreshCw className="mr-2 h-4 w-4" /> Retry
        </Button>
      </div>
    )
  }

  return (
    <div className="container mx-auto max-w-6xl space-y-4 p-4 md:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <GraduationCap className="h-7 w-7 text-primary" />
          <div>
            <h1 className="text-xl font-bold">FCC Academy</h1>
            <p className="text-sm text-muted-foreground">
              Learn with your live market data — education only, never advice.
            </p>
          </div>
        </div>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            const s = symbolDraft.trim().toUpperCase()
            setSymbol(s)
            if (selected) openLesson(selected)
          }}
        >
          <Input
            value={symbolDraft}
            onChange={(e) => setSymbolDraft(e.target.value.toUpperCase())}
            placeholder="SYMBOL"
            className="w-32 font-semibold uppercase"
            aria-label="Teaching symbol"
          />
          <Button type="submit" variant="outline" size="sm">
            Apply
          </Button>
        </form>
      </div>

      {!selected ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {lessons.map((l) => (
            <Card
              key={l.id}
              className="cursor-pointer transition-colors hover:bg-muted/50"
              onClick={() => openLesson(l)}
            >
              <CardContent className="flex items-start justify-between gap-3 pt-4">
                <div>
                  <div className="font-semibold">{l.title}</div>
                  <div className="mt-1 text-sm text-muted-foreground">{l.subtitle}</div>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  {l.live && (
                    <Badge variant="outline" className="text-profit border-profit/40">
                      LIVE
                    </Badge>
                  )}
                  <span className="text-xs text-muted-foreground">{l.minutes} min</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Button variant="ghost" size="sm" onClick={() => setSelected(null)}>
              ← All lessons
            </Button>
            {lesson && (
              <span className="text-xs text-muted-foreground">
                Live data for {lesson.data.symbol} [{lesson.data.exchange}] · fetched{' '}
                {lesson.data.fetched_at}
              </span>
            )}
          </div>

          {lessonLoading && (
            <div className="flex h-48 flex-col items-center justify-center gap-2">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
              <p className="text-sm text-muted-foreground">
                Preparing “{selected.title}” — fetching live data for {symbol.toUpperCase()}
              </p>
            </div>
          )}

          {lessonError && (
            <div className="flex h-48 flex-col items-center justify-center gap-3 text-center">
              <WifiOff className="h-7 w-7 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">Could not load the lesson: {lessonError}</p>
              <Button variant="outline" size="sm" onClick={() => openLesson(selected)}>
                <RefreshCw className="mr-2 h-4 w-4" /> Retry
              </Button>
            </div>
          )}

          {lesson && (
            <>
              <div>
                <h2 className="text-lg font-bold">{lesson.lesson.title}</h2>
                <p className="text-sm text-muted-foreground">{lesson.lesson.subtitle}</p>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                {lesson.data.quote && <QuoteCard data={lesson.data} />}
                {lesson.data.indicators && (
                  <IndicatorsCard ind={lesson.data.indicators} />
                )}
                {lesson.data.chain?.has_options === true && (
                  <ChainCard chain={lesson.data.chain} />
                )}
                {lesson.data.candles && lesson.data.candles.length > 0 && (
                  <CandlesCard candles={lesson.data.candles} />
                )}
              </div>

              <Card>
                <CardContent className="space-y-3 pt-4">
                  {lesson.explanation.map((p, i) => (
                    <p key={i} className="text-sm leading-relaxed">
                      {clean(p)}
                    </p>
                  ))}
                </CardContent>
              </Card>

              <Card>
                <CardContent className="pt-4">
                  <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Check yourself
                  </div>
                  <div className="space-y-4">
                    {lesson.quiz.map((q, i) => {
                      const res = grade?.results[i]
                      return (
                        <div key={i}>
                          <div className="text-sm font-semibold">
                            Q{i + 1}. {clean(q.question)}
                          </div>
                          <div className="mt-2 grid gap-1.5 sm:grid-cols-3">
                            {q.options.map((opt, oi) => {
                              const isPicked = answers[String(i)] === oi
                              const isRight = res?.answer === oi
                              const isWrongPick = res && res.given === oi && !res.correct
                              return (
                                <button
                                  key={oi}
                                  type="button"
                                  disabled={!!grade}
                                  onClick={() => setAnswers((a) => ({ ...a, [String(i)]: oi }))}
                                  className={cn(
                                    'rounded-lg border px-3 py-2 text-left text-sm transition-colors',
                                    isRight
                                      ? 'border-profit/60 bg-profit/10'
                                      : isWrongPick
                                        ? 'border-loss/60 bg-loss/10'
                                        : isPicked
                                          ? 'border-primary bg-primary/10'
                                          : 'hover:bg-muted/50'
                                  )}
                                >
                                  <span className="mr-1.5 font-bold text-muted-foreground">
                                    {String.fromCharCode(65 + oi)}
                                  </span>
                                  {clean(opt)}
                                </button>
                              )
                            })}
                          </div>
                          {res && (
                            <p
                              className={cn(
                                'mt-1.5 text-xs',
                                res.correct ? 'text-muted-foreground' : 'text-amber-500'
                              )}
                            >
                              {res.correct ? '' : 'Why: '}
                              {clean(res.why)}
                            </p>
                          )}
                        </div>
                      )
                    })}
                  </div>
                  <div className="mt-4 flex items-center gap-3">
                    {!grade ? (
                      <Button
                        onClick={submitQuiz}
                        disabled={grading || Object.keys(answers).length < lesson.quiz.length}
                      >
                        {grading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                        Check answers
                      </Button>
                    ) : (
                      <>
                        <Badge variant={grade.passed ? 'default' : 'destructive'}>
                          {grade.passed ? 'Passed' : 'Not yet'} — {grade.correct}/{grade.total} (
                          {grade.score}%)
                        </Badge>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setGrade(null)
                            setAnswers({})
                          }}
                        >
                          Retry quiz
                        </Button>
                      </>
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card className="border-primary/30">
                <CardContent className="pt-4">
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-primary">
                    Ask the tutor
                  </div>
                  <p className="mb-3 text-xs text-muted-foreground">
                    Anything about this lesson — answered with today's live numbers.
                  </p>
                  <div className="flex gap-2">
                    <Input
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') sendQuestion()
                      }}
                      placeholder="e.g. why does RSI stay high in a trend?"
                    />
                    <Button onClick={sendQuestion} disabled={asking || !question.trim()}>
                      {asking ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Send className="h-4 w-4" />
                      )}
                    </Button>
                  </div>
                  {answer && (
                    <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed">{clean(answer)}</p>
                  )}
                </CardContent>
              </Card>
            </>
          )}
        </div>
      )}
    </div>
  )
}
