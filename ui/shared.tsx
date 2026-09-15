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

// 签到台账一行：[日期, 当日实得金币, 是否幸运]（来自 `core/checkin.py` 的归一化形状）。
export type CheckinLogEntry = [string, number, boolean]

// 面板日历块：`snapshot.checkin` 基础数据 + `_checkin_context_view` 补的配置读数。
// 连续天数是后端从集合现算的显示值；前端不参与任何判据（与作息条同一分工）。
export type CheckinView = {
  today?: string
  checked_today?: boolean
  streak?: number
  best?: number
  log?: CheckinLogEntry[]
  makeups?: string[]
  enabled?: boolean
  feature_enabled?: boolean
  next_reward?: number
  makeup_cost?: number
  makeup_left?: number
  makeup_window_days?: number
  makeup_week_limit?: number
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

// 纪念日（core/rhythm.Anniversary.as_dict 的形状）：kind = milestone / yearly。
export type AnniversaryView = {
  kind?: string
  day_number?: number
  repeats_annually?: boolean
  years?: number
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
  anniversary?: AnniversaryView | null
  feedback?: FeedbackView
}

export type ShopEntry = {
  id?: string
  kind?: string
  cost?: number
  food?: boolean
  carry_max?: number
  effects?: [string, number][]
  // v0.7.0 商店深化：`price` 是后端今天会收的价（特惠日已折）；
  // `rarity`/`keepsake`/`daily` 是呈现层字段，解锁与折扣判定都不读它们。
  price?: number
  deal?: boolean
  discount_pct?: number
  rarity?: string
  keepsake?: boolean
  daily?: [string, number][]
}

// 打工面板块：`snapshot.job` 读数 + `_job_context_view` 补的目录与旋钮。
// 目录是静态真相（core/jobs.py 单一来源），计数是当次读数，两层在后端合流。
export type JobCatalogEntry = {
  id?: string
  label_zh?: string
  hours?: number
  base_wage?: number
  pay_low?: number
  pay_high?: number
  costs?: [string, number][]
  requires?: [string, number][]
}

export type JobView = {
  id?: string
  start_at?: number | null
  end_at?: number | null
  remaining_sec?: number
  today_count?: number
  shifts_total?: number
  earned_total?: number
  enabled?: boolean
  feature_enabled?: boolean
  max_per_day?: number
  early_leave_ratio?: number
  catalog?: JobCatalogEntry[]
}

// 小游戏（v0.7.0）：`active` 就是后端的**公开视图**（arith 只含题面、hielo 只含当前牌）——
// 题答与牌序从不出现在这里，重开面板也能续局（单一真相在 context）。
export type GameActiveView = {
  kind?: string
  issued_at?: number
  deadline_at?: number | null
  questions?: string[]
  current?: number | null
  revealed?: number[]
  round?: number
  rounds?: number
  wins?: number
  losses?: number
}

export type GamesView = {
  active?: GameActiveView | null
  counts?: Record<string, number>
  earned_today?: number
  earned_total?: number
  last?: Record<string, any>
  enabled?: boolean
  feature_enabled?: boolean
  per_game_daily_limit?: number
  total_daily_limit?: number
  arith?: Record<string, number>
  hielo?: Record<string, number>
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
  checkin?: CheckinView
  job?: JobView
  games?: GamesView
  axes?: Record<string, AxisView>
  /** 「她此刻的状态」页（v0.8.0）：判据在 `core/state_note.py`，这里只接结果。 */
  state_note?: StateNote
  recent_injections?: InjectionRecord[]
  recent_events?: EventRecord[]
  trend?: InjectionRecord[]
  hours?: number[]
  meal_days?: [string, number][]
  error_code?: string
}

// 「她此刻的状态」页的数据面（v0.8.0）。字段与 `core/state_note.build_state_note` 一一对应：
// **后端只出判据与键名/ASCII 码，文案全在 i18n**。`voice` 是整句键（不逐词拼），
// `coupling` 是 `camel` 后查 `panel.stateCoupling.<码>` 的稳定码。
// 所有字段可选：旧 context / 无分片时组件自己降级，不因为一处缺数据就整页报错。
export type StateNoteBodyRow = {
  stat?: string
  tier?: string
  tier_index?: number
  value?: number
  next_tier?: string | null
  to_next?: number | null
  delta_today?: number | null
}

export type StateNote = {
  voice?: string[]
  voice_count?: number
  voice_reason?: string
  body?: StateNoteBodyRow[]
  coupling?: string[]
  rhythm?: {
    phase?: string | null
    sleeping?: boolean | null
    hour?: number | null
    minute?: number | null
    hours_to_sleep?: number | null
    hours_to_wake?: number | null
  }
  us?: {
    day_number?: number | null
    streak_days?: number | null
    gap_hours?: number | null
    anniversary_kind?: string | null
  }
  mind?: {
    has_judgment?: boolean
    label?: string | null
    applied?: number | null
    count_today?: number | null
    at?: number | null
  }
  today?: {
    meals_today?: number | null
    checked_today?: boolean
    job_id?: string | null
    job_remaining_sec?: number | null
    game_active?: boolean
    sodas?: number | null
    spoke_today?: number | null
  }
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
