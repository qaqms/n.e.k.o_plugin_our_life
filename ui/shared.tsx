// 面板共享层（v0.6.0 拆分）：类型 + 纯函数工具。
//
// 为什么单独成文件：`ui/panel.tsx` 曾长到 900+ 行，所有区块焊在一起、页面却稀疏；
// 拆分后骨架留在 panel.tsx，可视模块在 ui/components/**，这里只放**两边都要用**的东西
// （对偶性纪律：两处需要的逻辑只写一遍）。
//
// hosted-tsx 约束照旧适用于本文件：只 import `@neko/plugin-ui` 与相对路径；
// 只用具名导出；注释里也不许出现"全局门面"的裸标识符形态（检查器是文本级的）。

import type { PluginSurfaceProps } from "@neko/plugin-ui"

export type Translate = (key: string, params?: Record<string, any>) => string

export type ShardSnapshot = {
  lanlan?: string
  affection?: number
  mood?: number
  health?: number
  satiety?: number
  energy?: number
  tiers?: Record<string, string>
  streak_days?: number
  day_number?: number
  last_touch_at?: number | null
  gap_hours?: number | null
  last_inject_at?: number | null
  inject_count_24h?: number
  sodas?: number
  daily_spent?: number
  meals_total?: number
  meals_today?: number
  last_meal_at?: number | null
  sleeping?: boolean
  inventory?: Record<string, number>
  events?: { history?: EventRecord[]; total?: number }
}

export type InjectionRecord = {
  at?: number
  trigger?: string
  summary?: string
  stats?: Record<string, number>
}

export type AdvisorView = {
  meal_need_per_day?: number
  observed_meals_per_day?: number
  meals_per_day?: number
  stock_meals?: number
  days_remaining?: number | null
  horizon_days?: number
  shortfall_meals?: number
  suggested_purchase?: number
  suggested_cost?: number
  affordable?: boolean
  urgent?: boolean
  empty?: boolean
}

export type JudgmentRecord = {
  at?: number
  label?: string
  applied?: number
}

export type FeedbackView = {
  enabled?: boolean
  daily_add_points?: number
  daily_subtract_points?: number
  used_add?: number
  used_subtract?: number
  remaining_add?: number
  remaining_subtract?: number
  count_today?: number
  last_judgment_at?: number | null
  history?: JudgmentRecord[]
}

export type RuntimeView = {
  rhythm?: Record<string, any>
  phase?: string
  sleeping?: boolean
  minutes_to_boundary?: number
  per_day_decay?: number
  advisor?: AdvisorView
  crisis?: boolean
  crisis_axes?: string[]
  day_number?: number
  anniversary?: { kind?: string; day_number?: number; years?: number } | null
  feedback?: FeedbackView
}

export type ShopEntry = {
  id?: string
  kind?: string
  cost?: number
  food?: boolean
  carry_max?: number
  effects?: [string, number][]
}

// 她经历过什么（v0.4.0 阶段性事件）。
// 只带事件名 / 轴 / 时刻与数值快照——台账里本来就没有任何对话正文（见 core/events.py）。
export type EventRecord = {
  key?: string
  stat?: string
  value?: number
  width?: number
  at?: number
  [key: string]: unknown
}

// 五轴卡明细（v0.6.0，由 core.model.axis_details 现算；档位线唯一来源是 TIER_BOUNDS）。
export type AxisView = {
  value?: number
  tier?: string
  tier_index?: number
  next_tier?: string | null
  to_next?: number | null
  delta_today?: number | null
}

export type State = {
  enabled?: boolean
  lanlan?: string
  shards?: string[]
  store_available?: boolean
  config?: Record<string, any>
  shop?: ShopEntry[]
  state?: ShardSnapshot | null
  runtime?: RuntimeView
  axes?: Record<string, AxisView>
  recent_injections?: InjectionRecord[]
  recent_events?: EventRecord[]
  trend?: InjectionRecord[]
  hours?: number[]
  meal_days?: [string, number][]
  error_code?: string
}

export type PanelProps = PluginSurfaceProps<State>

export const STAT_KEYS = ["energy", "satiety", "mood", "health", "affection"] as const

// Hosted UI Kit 没有图表组件，走势一律用字符画折线（▁▂▃▄▅▆▇█）。
export const TREND_LEVELS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

/** 档位下标 → 徽章色调：越接近坏档越暖。这是呈现规则，不是判据（判据在档位表）。 */
export function tierTone(index: number | undefined): "danger" | "warning" | "success" | "default" {
  if (index === undefined) return "default"
  if (index <= 0) return "danger"
  if (index === 1) return "warning"
  if (index >= 3) return "success"
  return "default"
}

export function camel(code: string): string {
  return code
    .split("_")
    .filter(Boolean)
    .map((part, index) => (index === 0 ? part : part.charAt(0).toUpperCase() + part.slice(1)))
    .join("")
}

/** 把后端抛出的东西翻成用户能读的一句话：稳定码走 i18n，其它原样直出。 */
export function errorText(error: unknown, t: Translate): string {
  const raw = error instanceof Error ? error.message : String(error ?? "")
  if (/^[a-z][a-z0-9_]*$/.test(raw)) {
    return t(`panel.errors.${camel(raw)}`, { defaultValue: raw })
  }
  return raw
}

export function envelopeResult(envelope: any): any {
  return envelope && typeof envelope === "object" && "result" in envelope ? envelope.result : envelope
}

export function formatTime(seconds?: number | null, fallback?: string): string {
  if (!seconds) return fallback ?? ""
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return fallback ?? ""
  return date.toLocaleString()
}

export function formatGap(hours: number | null | undefined, t: Translate): string {
  if (hours === null || hours === undefined) return t("panel.never")
  if (hours < 1) return t("panel.gapMinutes", { minutes: Math.max(1, Math.round(hours * 60)) })
  if (hours < 24) return t("panel.gapHours", { hours: Math.round(hours) })
  return t("panel.gapDays", { days: Math.floor(hours / 24) })
}

/** 把 0..100 的数值映射成一格字符，用于 CSS/字符画走势。 */
export function sparkline(values: number[]): string {
  if (values.length === 0) return ""
  return values
    .map((value) => {
      const clamped = Math.max(0, Math.min(100, Number(value)))
      const index = Math.min(TREND_LEVELS.length - 1, Math.floor((clamped / 100) * TREND_LEVELS.length))
      return TREND_LEVELS[index]
    })
    .join("")
}

/** 把任意计数序列归一成字符柱（作息条用：柱高相对当日最高峰，全 0 就全空格）。 */
export function hourBars(counts: number[]): string[] {
  const peak = counts.reduce((max, count) => Math.max(max, Number(count) || 0), 0)
  if (peak <= 0) return counts.map(() => "·")
  return counts.map((count) => {
    const value = Number(count) || 0
    if (value <= 0) return "·"
    const level = Math.min(TREND_LEVELS.length - 1, Math.floor((value / peak) * (TREND_LEVELS.length - 1)))
    return TREND_LEVELS[level]
  })
}
