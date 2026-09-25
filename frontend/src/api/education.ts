/**
 * FCC Academy — live education API.
 *
 * Served by services/fcc_education_service.py via blueprints/fcc_ai.py under
 * /plugins/fcc/education/*. Session-authenticated like the other plugin
 * surfaces, so this uses webClient (cookie + CSRF), matching the scalper
 * orderflow client beside it.
 */

import { webClient } from './client'

export interface EduLessonMeta {
  id: string
  title: string
  subtitle: string
  minutes: number
  live: boolean
}

export interface EduQuizQuestion {
  question: string
  options: string[]
}

export interface EduQuizResult {
  index: number
  question: string
  given: number | null
  answer: number
  correct: boolean
  why: string
}

export interface EduLessonData {
  symbol: string
  exchange: string
  fetched_at: string
  quote?: {
    ltp: number
    change: number
    change_pct: number
    open: number
    high: number
    low: number
    prev_close: number
  }
  depth?: Record<string, unknown>
  candles?: Array<Record<string, unknown>>
  indicators?: Record<string, unknown>
  chain?: Record<string, unknown>
}

export interface EduLesson {
  lesson: EduLessonMeta
  data: EduLessonData
  explanation: string[]
  quiz: EduQuizQuestion[]
}

export interface EduGrade {
  score: number
  correct: number
  total: number
  passed: boolean
  results: EduQuizResult[]
}

export async function getCurriculum(): Promise<EduLessonMeta[]> {
  const response = await webClient.get('/plugins/fcc/education/curriculum')
  return (response.data?.lessons ?? []) as EduLessonMeta[]
}

export async function getLesson(
  lessonId: string,
  symbol?: string,
  exchange?: string
): Promise<EduLesson> {
  const params: Record<string, string> = {}
  if (symbol) params.symbol = symbol
  if (exchange) params.exchange = exchange
  const response = await webClient.get(`/plugins/fcc/education/lesson/${lessonId}`, { params })
  return response.data as EduLesson
}

export async function postQuiz(
  lessonId: string,
  answers: Record<string, number>
): Promise<EduGrade> {
  const response = await webClient.post(`/plugins/fcc/education/quiz/${lessonId}`, {
    answers,
  })
  return response.data as EduGrade
}

export async function askTutor(body: {
  question: string
  lesson?: string
  symbol?: string
  exchange?: string
}): Promise<{ answer: string; model?: string }> {
  const response = await webClient.post('/plugins/fcc/education/ask', body)
  return response.data as { answer: string; model?: string }
}

export async function getProgress(): Promise<{
  ephemeral: boolean
  lessons: Record<string, { best_score: number; attempts: number }>
  completed: number
}> {
  const response = await webClient.get('/plugins/fcc/education/progress')
  return response.data
}
