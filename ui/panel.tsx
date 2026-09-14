// Hosted TSX 面板：只从 `@neko/plugin-ui` 导入，业务逻辑全在 Python 侧。
//
// 契约要点（照 `plugin/sdk/hosted-ui/index.d.ts` 的精确签名写，不照文档猜）：
// - `Grid` 的列数是 `cols`（不是 columns）；`Select` / `NumberInput` **没有** `label`，要包 `Field`；
//   `StatusBadge` 用 `label`（不是 text）；`Tone` 只有 primary/success/warning/danger/info/default。
// - 动作调用返回的是**信封** `{plugin_id, action_id, result}`，真正的返回值在 `.result`。
// - 只允许相对导入与 `@neko/plugin-ui`；必须 `export default` 一个函数组件。
// - 检查器是**文本级**规则：文件里出现"全局门面对象"的裸标识符形态就会被拒收，
//   所以一律写完整的 `props.api` 成员访问（连注释也不例外）。
//
// 布局（v0.4.1 重构，替代旧的"一条道滑到底"）：
// - **顶部状态带**：五轴进度条 + 今日事实徽章（第几天 / 连续天数 / 时段 / 睡眠 / 危机）+ 总开关，
//   滚到任何角落她当前的状态都一眼可见；
// - **Tabs 四页**：总览（走势 + 相处节律）/ 过日子（口粮顾问 + 商店 + 背包）/
//   她的世界（她自己的感受 + 她经历过什么）/ 管理（纠偏 + 近期注入 + 配置）。
//   Tab 激活态由 kit 的 `useLocalState("tabs:<id>")` 持久化，刷新上下文不丢位置。
// - **自动刷新（v0.4.3）**：状态带里的手动「刷新」按钮已退役，面板每 10s 自动拉一次
//   context（带防重入 / 后台暂停 / 回可见补拉三条性能闸门），见 Panel 内注释。
import {
  Alert,
  Button,
  Card,
  Columns,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Inline,
  KeyValue,
  NumberInput,
  Page,
  Progress,
  SegmentedControl,
  Select,
  Slider,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Tabs,
  Text,
  useConfirm,
  useEffect,
  useLocalState,
  useRef,
  useToast,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

type ShardSnapshot = {
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
}

type InjectionRecord = {
  at?: number
  trigger?: string
  summary?: string
  stats?: Record<string, number>
}

type RhythmView = {
  phase?: string
  sleeping?: boolean
  minutes_to_boundary?: number
  per_day_decay?: number
}

type AdvisorView = {
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

type JudgmentRecord = {
  at?: number
  label?: string
  applied?: number
}

type FeedbackView = {
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

type RuntimeView = {
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

type ShopEntry = {
  id?: string
  kind?: string
  cost?: number
  food?: boolean
  carry_max?: number
  effects?: [string, number][]
}

/**
 * 她经历过什么（v0.4.0 阶段性事件）。
 * 只带事件名 / 轴 / 时刻与数值快照——台账里本来就没有任何对话正文（见 core/events.py）。
 */
type EventRecord = {
  key?: string
  stat?: string
  value?: number
  width?: number
  at?: number
  [key: string]: unknown
}

type State = {
  enabled?: boolean
  lanlan?: string
  shards?: string[]
  store_available?: boolean
  config?: Record<string, any>
  shop?: ShopEntry[]
  state?: ShardSnapshot | null
  runtime?: RuntimeView
  recent_injections?: InjectionRecord[]
  recent_events?: EventRecord[]
  trend?: InjectionRecord[]
  hours?: number[]
  meal_days?: [string, number][]
  error_code?: string
}

const STAT_KEYS = ["energy", "satiety", "mood", "health", "affection"] as const

// 自动轮询节奏（v0.4.3）：见 Panel 内「自动刷新」注释段的三条性能闸门论证。
const AUTO_REFRESH_MS = 10000
const TREND_LEVELS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

function camel(code: string): string {
  return code
    .split("_")
    .filter(Boolean)
    .map((part, index) => (index === 0 ? part : part.charAt(0).toUpperCase() + part.slice(1)))
    .join("")
}

/** 把后端抛出的东西翻成用户能读的一句话：稳定码走 i18n，其它原样直出。 */
function errorText(error: unknown, t: (key: string, params?: Record<string, any>) => string): string {
  const raw = error instanceof Error ? error.message : String(error ?? "")
  if (/^[a-z][a-z0-9_]*$/.test(raw)) {
    return t(`panel.errors.${camel(raw)}`, { defaultValue: raw })
  }
  return raw
}

function envelopeResult(envelope: any): any {
  return envelope && typeof envelope === "object" && "result" in envelope ? envelope.result : envelope
}

function formatTime(seconds?: number | null, fallback?: string): string {
  if (!seconds) return fallback ?? ""
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return fallback ?? ""
  return date.toLocaleString()
}

function formatGap(hours: number | null | undefined, t: (k: string, p?: Record<string, any>) => string): string {
  if (hours === null || hours === undefined) return t("panel.never")
  if (hours < 1) return t("panel.gapMinutes", { minutes: Math.max(1, Math.round(hours * 60)) })
  if (hours < 24) return t("panel.gapHours", { hours: Math.round(hours) })
  return t("panel.gapDays", { days: Math.floor(hours / 24) })
}

/** 把 0..100 的数值映射成一格字符，用于 CSS/字符画走势（Hosted UI Kit 没有图表组件）。 */
function sparkline(values: number[]): string {
  if (values.length === 0) return ""
  return values
    .map((value) => {
      const clamped = Math.max(0, Math.min(100, Number(value)))
      const index = Math.min(TREND_LEVELS.length - 1, Math.floor((clamped / 100) * TREND_LEVELS.length))
      return TREND_LEVELS[index]
    })
    .join("")
}

export default function Panel(props: PluginSurfaceProps<State>) {
  // 注意：**不要**从 props 里解构出那个"调用门面"的短名字——hosted-tsx 检查器是
  // 文本级规则（连注释都不跳过），一旦文件里出现它的裸标识符形态就会被判成
  // "用了全局对象"而拒收（catgirl_seiyuu 台账里踩过同一个坑）。
  // 一律写完整的成员访问形式。
  const { t, state } = props
  const toast = useToast()
  const confirm = useConfirm()
  const [tuneStat, setTuneStat] = useLocalState<string>("our_life.tune.stat", "mood")
  const [tuneValue, setTuneValue] = useLocalState<number | "">("our_life.tune.value", 50)
  const [buyItem, setBuyItem] = useLocalState<string>("our_life.buy.item", "meat")
  const [buyQuantity, setBuyQuantity] = useLocalState<number | "">("our_life.buy.quantity", 3)
  const [careItem, setCareItem] = useLocalState<string>("our_life.care.item", "meat")

  const snapshot = state?.state ?? null
  const runtime = state?.runtime ?? {}
  const advisor = runtime.advisor ?? {}
  const feedback: FeedbackView = runtime.feedback ?? {}
  const judgmentHistory = feedback.history ?? []
  const eventHistory = state?.recent_events ?? []
  const enabled = state?.enabled === true
  const config = state?.config ?? {}
  const tiers = snapshot?.tiers ?? {}
  const inventory = snapshot?.inventory ?? {}
  const catalog = state?.shop ?? []
  const sleeping = runtime.sleeping ?? snapshot?.sleeping ?? false

  const run = async (actionId: string, args: Record<string, any>) => {
    try {
      const envelope = await props.api.call(actionId, args, { timeoutMs: 20000 })
      return envelopeResult(envelope)
    } catch (error) {
      toast.error(errorText(error, t))
      return null
    }
  }

  const dismissResult = (result: any) => {
    if (!result) return false
    if (result.note === "store_unavailable") {
      toast.error(t("panel.errors.storeUnavailable"))
      return true
    }
    return false
  }

  const toggleEnabled = async (next: boolean) => {
    const result = await run("switch", { enabled: next })
    if (result?.note === "enabled" || result?.note === "disabled") {
      toast.success(t(`panel.msg.${camel(String(result.note))}`, { defaultValue: String(result.note) }))
    }
  }

  const applyTune = async () => {
    if (tuneValue === "" || Number.isNaN(Number(tuneValue))) {
      toast.warning(t("panel.msg.invalidValue"))
      return
    }
    const result = await run("tune", { stat: tuneStat, value: Number(tuneValue) })
    if (dismissResult(result)) return
    if (result?.note === "stat_updated") {
      toast.success(t("panel.msg.statUpdated"))
    }
  }

  const buy = async () => {
    const quantity = Number(buyQuantity)
    if (buyQuantity === "" || Number.isNaN(quantity) || quantity <= 0) {
      toast.warning(t("panel.errors.invalidQuantity"))
      return
    }
    const result = await run("shop", { item: buyItem, quantity })
    if (dismissResult(result)) return
    if (result?.note === "shop_purchased") {
      toast.success(t("panel.msg.shopPurchased"))
    }
  }

  const care = async () => {
    const result = await run("feed", { item: careItem })
    if (dismissResult(result)) return
    if (result?.note === "care_applied") {
      toast.success(t("panel.msg.careApplied"))
    }
  }

  const resetStats = async () => {
    const ok = await confirm({
      title: t("panel.resetConfirm.title"),
      message: t("panel.resetConfirm.message"),
      tone: "danger",
      confirmLabel: t("panel.resetConfirm.confirm"),
      cancelLabel: t("panel.resetConfirm.cancel"),
    })
    if (!ok) return
    const result = await run("reset", {})
    if (result?.note === "stats_reset") {
      toast.success(t("panel.msg.statsReset"))
    }
  }

  // 面板焦点切换（v0.4.2）：多张卡时唯一能选"看哪张"的途径——
  // 宿主调 `@ui.context` 是不带任何角色信息的，后台无法替用户决定。
  // "auto" 段发送空串清除焦点，退回"全局唯一分片才认"的自动判定。
  const shardNames = state?.shards ?? []
  const switchFocus = async (next: any) => {
    const wanted = String(next ?? "")
    const result = await run("focus", { lanlan: wanted === "auto" ? "" : wanted })
    if (dismissResult(result)) return
    if (result?.note === "focus_set") {
      toast.success(t("panel.msg.focusSet"))
    } else if (result?.note === "focus_cleared") {
      toast.success(t("panel.msg.focusCleared"))
    }
    await props.api.refresh()
  }

  // 自动刷新（v0.4.3）：手动「刷新」按钮退役——面板数值是后端 tick 结算后、
  // 读取时刻按真实时间折算的结果，让用户按按钮去拉快照等于让用户充当定时器。
  // 三条性能闸门：
  // - 防重入：上一轮 refresh 未返回就跳过本拍（不排队、不叠加请求）；
  // - 后台暂停：document.hidden（面板不可见）时不拉数据；
  // - 回可见补拉：从后台切回来立刻补一轮，再恢复常态节奏。
  // 10s 与 tick_seconds=30 的结算周期同量级：面板最多滞后 10 秒；她「主动开口」
  // 走 push 通道、不依赖这里，轮询只服务面板观察。动作自带 refresh_context=True，
  // 成功反馈的即时刷新由宿主负责，这里只管「没有人操作时数据也不旧」。
  const refreshBusy = useRef(false)
  useEffect(() => {
    const hidden = () => typeof document !== "undefined" && document.hidden === true
    const tick = async () => {
      if (refreshBusy.current || hidden()) return
      refreshBusy.current = true
      try {
        await props.api.refresh()
      } catch {
        // 轮询失败静默忽略、下拍重试：面板可能开着没人看，这时弹 toast 只是噪音。
      } finally {
        refreshBusy.current = false
      }
    }
    const onVisibility = () => {
      if (!hidden()) void tick()
    }
    const timer = setInterval(tick, AUTO_REFRESH_MS)
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibility)
    return () => {
      clearInterval(timer)
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [])

  const statOptions = STAT_KEYS.map((key) => ({ value: key, label: t(`panel.stat.${key}`) }))
  const catalogItems = catalog.map((entry) => ({
    value: String(entry.id ?? ""),
    label: t(`panel.item.${entry.id ?? "unknown"}`, { defaultValue: String(entry.id ?? "-") }),
  }))
  const affordableItems = catalogItems.filter((entry) => (inventory[entry.value] ?? 0) > 0)

  const hours = state?.hours ?? []
  const activeHours = hours
    .map((count, hour) => ({ count, hour }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count)
    .slice(0, 6)

  const injections = state?.recent_injections ?? []
  const trend = state?.trend ?? []
  const trendPoints = (trend.length > 0 ? trend : injections.filter((row) => row.stats)).slice(-12)

  const crisisAxes = (runtime.crisis_axes ?? [])
    .map((axis) => t(`panel.stat.${axis}`, { defaultValue: axis }))
    .join(" · ")

  const effectText = (entry: ShopEntry): string =>
    (entry.effects ?? [])
      .map(([name, delta]) => `${t(`panel.stat.${name}`, { defaultValue: name })} ${delta > 0 ? "+" : ""}${delta}`)
      .join(" · ")

  const daysRemaining = advisor.days_remaining
  const advisorLine =
    daysRemaining === null || daysRemaining === undefined
      ? t("panel.advisor.stock", { units: advisor.stock_meals ?? 0 })
      : t("panel.advisor.days", { days: daysRemaining })

  const tierOf = (key: string): string =>
    t(`panel.tier.${key}.${tiers[key] ?? "unknown"}`, { defaultValue: tiers[key] ?? "-" })

  // 多卡时的焦点切换器；单卡不占地方（自动判定已够）。
  // options 在 JSX 外构造：面板门是文本级的，标签内联对象字面量的 `label:`
  // 会被误判成"给组件传了它没有的 prop"（catalogItems/statOptions 同理）。
  const focusOptions =
    shardNames.length > 1
      ? [{ value: "auto", label: t("panel.focusAuto") }, ...shardNames.map((name) => ({ value: name, label: name }))]
      : []
  const focusSwitcher =
    shardNames.length > 1 ? (
      <Inline gap={8} align="center">
        <Text>{t("panel.field.lanlan")}</Text>
        <SegmentedControl
          value={snapshot?.lanlan && state?.lanlan ? snapshot.lanlan : "auto"}
          options={focusOptions}
          onChange={(next: any) => switchFocus(next)}
        />
      </Inline>
    ) : null

  // ---- 标签页内容 ----------------------------------------------------------
  // 每个 tab 的 content 在渲染期一并构造：kit 的 Tabs 只渲染激活页的 content，
  // 构造本身是纯读快照的 JSX，没有副作用。

  const overviewTab = (
    <Stack gap={16}>
      <Card title={t("panel.section.trend")}>
        {trendPoints.length > 1 ? (
          <Stack gap={8}>
            {STAT_KEYS.map((key) => (
              <Inline key={key} gap={12} align="center">
                <Text>{`${t(`panel.stat.${key}`)}`}</Text>
                <Text>{sparkline(trendPoints.map((row) => Number(row.stats?.[key] ?? 0)))}</Text>
                <Text>{`${Number(trendPoints[trendPoints.length - 1]?.stats?.[key] ?? 0).toFixed(0)}`}</Text>
              </Inline>
            ))}
          </Stack>
        ) : (
          <EmptyState title={t("panel.section.trend")} description={t("panel.trendHint")} />
        )}
      </Card>

      <Card title={t("panel.section.rhythm")}>
        <Stack gap={12}>
          <KeyValue
            items={[
              { key: "lanlan", label: t("panel.field.lanlan"), value: snapshot?.lanlan ?? "-" },
              {
                key: "phase",
                label: t("panel.field.phase"),
                value: t(`panel.phase.${runtime.phase ?? "noon"}`, { defaultValue: runtime.phase ?? "-" }),
              },
              {
                key: "boundary",
                label: sleeping ? t("panel.field.boundaryWake") : t("panel.field.boundarySleep"),
                value: String(runtime.minutes_to_boundary ?? 0),
              },
              { key: "dayNumber", label: t("panel.field.dayNumber"), value: String(runtime.day_number ?? 0) },
              { key: "streak", label: t("panel.field.streak"), value: String(snapshot?.streak_days ?? 0) },
              {
                key: "anniversary",
                label: t("panel.field.anniversary"),
                value: runtime.anniversary
                  ? runtime.anniversary.kind === "yearly"
                    ? t("panel.anniversary.yearly", { years: runtime.anniversary.years ?? 1 })
                    : t("panel.anniversary.milestone", { day: runtime.anniversary.day_number ?? 0 })
                  : "-",
              },
              {
                key: "last",
                label: t("panel.field.lastTouch"),
                value: formatTime(snapshot?.last_touch_at, t("panel.never")),
              },
              { key: "gap", label: t("panel.field.gap"), value: formatGap(snapshot?.gap_hours, t) },
              {
                key: "injects",
                label: t("panel.field.inject24h"),
                value: String(snapshot?.inject_count_24h ?? 0),
              },
            ]}
          />
          {activeHours.length > 0 ? (
            <Inline gap={8} wrap>
              {activeHours.map((item) => (
                <Text key={item.hour}>{`${String(item.hour).padStart(2, "0")}:00 ×${item.count}`}</Text>
              ))}
            </Inline>
          ) : (
            <Text>{t("panel.hoursHint")}</Text>
          )}
        </Stack>
      </Card>
    </Stack>
  )

  const lifeTab = (
    <Stack gap={16}>
      <Card title={t("panel.section.advisor")}>
        <Stack gap={12}>
          {/* 三个关键读数先立起来， KeyValue 长列表退到其次——"还够不够吃"不该藏在第七行。 */}
          <Columns cols={3} minWidth={140} gap={12}>
            <StatCard label={t("panel.field.sodas")} value={String(snapshot?.sodas ?? 0)} />
            <StatCard
              label={t("panel.field.staple")}
              value={
                daysRemaining === null || daysRemaining === undefined
                  ? t("panel.advisor.stock", { units: advisor.stock_meals ?? 0 })
                  : t("panel.advisor.days", { days: daysRemaining })
              }
            />
            <StatCard label={t("panel.field.mealsToday")} value={String(snapshot?.meals_today ?? 0)} />
          </Columns>
          <KeyValue
            items={[
              { key: "sodas", label: t("panel.field.sodas"), value: String(snapshot?.sodas ?? 0) },
              { key: "spent", label: t("panel.field.dailySpent"), value: String(snapshot?.daily_spent ?? 0) },
              {
                key: "need",
                label: t("panel.field.economy"),
                value: t("panel.advisor.need", { meals: advisor.meals_per_day ?? 0 }),
              },
              { key: "stock", label: t("panel.field.staple"), value: advisorLine },
              { key: "mealsToday", label: t("panel.field.mealsToday"), value: String(snapshot?.meals_today ?? 0) },
              { key: "mealsTotal", label: t("panel.field.mealsTotal"), value: String(snapshot?.meals_total ?? 0) },
              { key: "lastMeal", label: t("panel.field.lastMeal"), value: formatTime(snapshot?.last_meal_at, t("panel.never")) },
            ]}
          />
          <Text>
            {t("panel.advisor.hint", {
              meals: advisor.meals_per_day ?? 0,
              horizon: advisor.horizon_days ?? 0,
              units: (advisor.stock_meals ?? 0) + (advisor.suggested_purchase ?? 0),
            })}
          </Text>
          {advisor.empty ? <Alert tone="danger" message={t("panel.advisor.empty")} /> : null}
        </Stack>
      </Card>

      <Grid cols={2} gap={16}>
        <Card title={t("panel.section.shop")}>
          <Stack gap={12}>
            <Columns cols={2} minWidth={190} gap={12}>
              {catalog.map((entry) => (
                <Card key={String(entry.id)} title={t(`panel.item.${entry.id ?? "unknown"}`, { defaultValue: entry.id ?? "-" })}>
                  <Stack gap={8}>
                    <Text>{`${t("panel.field.sodas")} ${entry.cost ?? "-"}`}</Text>
                    <Text>{t("panel.shopEffects", { effects: effectText(entry) })}</Text>
                    <Text>
                      {(inventory[String(entry.id)] ?? 0) > 0
                        ? t("panel.shopOwned", { count: inventory[String(entry.id)] ?? 0 })
                        : t("panel.shopOutOfStock")}
                    </Text>
                    <Button
                      tone="success"
                      disabled={!enabled}
                      onClick={async () => {
                        const result = await run("shop", { item: entry.id, quantity: 1 })
                        if (dismissResult(result)) return
                        if (result?.note === "shop_purchased") toast.success(t("panel.msg.shopPurchased"))
                      }}
                    >
                      {t("actions.shop.label")}
                    </Button>
                  </Stack>
                </Card>
              ))}
            </Columns>
            <Field label={t("fields.item")}>
              <Select value={buyItem} options={catalogItems} onChange={(next: any) => setBuyItem(String(next))} />
            </Field>
            <Field label={t("fields.quantity")}>
              <NumberInput
                value={buyQuantity}
                min={1}
                max={99}
                step={1}
                onChange={(next: number | string) => setBuyQuantity(next === "" ? "" : Number(next))}
              />
            </Field>
            <Inline gap={12}>
              <Button tone="success" disabled={!enabled} onClick={buy}>
                {t("actions.shop.label")}
              </Button>
            </Inline>
          </Stack>
        </Card>

        <Card title={t("panel.section.bag")}>
          <Stack gap={12}>
            {affordableItems.length > 0 ? (
              <KeyValue
                items={affordableItems.map((entry) => ({
                  key: entry.value,
                  label: entry.label,
                  value: String(inventory[entry.value] ?? 0),
                }))}
              />
            ) : (
              <EmptyState title={t("panel.section.bag")} description={t("panel.advisor.empty")} />
            )}
            {affordableItems.length > 0 ? (
              <>
                <Field label={t("fields.item")}>
                  <Select
                    value={careItem}
                    options={affordableItems}
                    onChange={(next: any) => setCareItem(String(next))}
                  />
                </Field>
                <Inline gap={12}>
                  <Button tone="primary" disabled={!enabled} onClick={care}>
                    {t("actions.feed.label")}
                  </Button>
                </Inline>
              </>
            ) : null}
          </Stack>
        </Card>
      </Grid>
    </Stack>
  )

  const herTab = (
    <Stack gap={16}>
      <Card title={t("panel.section.feedback")}>
        <Stack gap={12}>
          <KeyValue
            items={[
              {
                key: "count",
                label: t("panel.field.feedbackCount"),
                value: String(feedback.count_today ?? 0),
              },
              {
                key: "quota",
                label: t("panel.field.feedbackQuota"),
                value: `${(feedback.remaining_add ?? 0).toFixed(1)} / ${(
                  feedback.remaining_subtract ?? 0
                ).toFixed(1)}`,
              },
              {
                key: "last",
                label: t("panel.field.feedbackLast"),
                value: formatTime(feedback.last_judgment_at, t("panel.never")),
              },
            ]}
          />
          {judgmentHistory.length > 0 ? (
            <DataTable
              rowKey="at"
              data={judgmentHistory}
              emptyText={t("panel.never")}
              columns={[
                {
                  key: "at",
                  label: t("panel.field.time"),
                  render: (row: JudgmentRecord) => formatTime(row.at, "-"),
                },
                {
                  key: "label",
                  label: t("panel.field.judgment"),
                  render: (row: JudgmentRecord) =>
                    t(`panel.judgment.${row.label ?? "neutral"}`, {
                      defaultValue: row.label ?? "-",
                    }),
                },
              ]}
            />
          ) : (
            <Text>{t("panel.feedbackHint")}</Text>
          )}
        </Stack>
      </Card>

      <Card title={t("panel.section.events")}>
        {eventHistory.length > 0 ? (
          <Stack gap={12}>
            <DataTable
              rowKey="at"
              data={eventHistory}
              emptyText={t("panel.noEvents")}
              columns={[
                {
                  key: "at",
                  label: t("panel.field.time"),
                  render: (row: EventRecord) => formatTime(row.at, "-"),
                },
                {
                  key: "key",
                  label: t("panel.field.event"),
                  render: (row: EventRecord) =>
                    t(`panel.event.${row.key ?? "unknown"}`, { defaultValue: row.key ?? "-" }),
                },
                {
                  key: "stat",
                  label: t("panel.field.stat"),
                  render: (row: EventRecord) =>
                    t(`panel.stat.${row.stat ?? "unknown"}`, { defaultValue: row.stat ?? "-" }),
                },
              ]}
            />
            <Text>{t("panel.eventsHint")}</Text>
          </Stack>
        ) : (
          <EmptyState title={t("panel.noEvents")} description={t("panel.noEventsHint")} />
        )}
      </Card>
    </Stack>
  )

  const adminTab = (
    <Stack gap={16}>
      <Card title={t("panel.section.tune")}>
        <Stack gap={12}>
          <Field label={t("panel.field.stat")} help={t("panel.tuneHelp")}>
            <Select value={tuneStat} options={statOptions} onChange={(next: any) => setTuneStat(String(next))} />
          </Field>
          <Field label={t("panel.field.value")}>
            <Slider
              value={Number(tuneValue === "" ? 0 : tuneValue)}
              min={0}
              max={100}
              step={1}
              showValue
              onChange={(next: number) => setTuneValue(next)}
            />
          </Field>
          <Inline gap={12}>
            <Button tone="primary" disabled={!enabled} onClick={applyTune}>
              {t("actions.tune.label")}
            </Button>
            <Button tone="danger" onClick={resetStats}>
              {t("actions.reset.label")}
            </Button>
          </Inline>
        </Stack>
      </Card>

      <Card title={t("panel.section.history")}>
        {injections.length > 0 ? (
          <DataTable
            rowKey="at"
            data={injections}
            emptyText={t("panel.noInjections")}
            columns={[
              {
                key: "at",
                label: t("panel.field.time"),
                render: (row: InjectionRecord) => formatTime(row.at, "-"),
              },
              {
                key: "trigger",
                label: t("panel.field.trigger"),
                render: (row: InjectionRecord) =>
                  t(`panel.trigger.${row.trigger ?? "unknown"}`, { defaultValue: row.trigger ?? "-" }),
              },
              { key: "summary", label: t("panel.field.summary") },
            ]}
          />
        ) : (
          <EmptyState title={t("panel.noInjections")} description={t("panel.noInjectionsHint")} />
        )}
      </Card>

      <Card title={t("panel.section.config")}>
        <KeyValue
          items={[
            { key: "tick", label: t("panel.field.tick"), value: String(config.tick_seconds ?? "-") },
            {
              key: "sleep",
              label: t("panel.field.sleepWindow"),
              value: `${config.sleep_start_hour ?? "-"}:00 → ${config.sleep_end_hour ?? "-"}:00`,
            },
            { key: "moodTau", label: t("panel.field.moodTau"), value: String(config.mood_tau_hours ?? "-") },
            { key: "healthTau", label: t("panel.field.healthTau"), value: String(config.health_tau_hours ?? "-") },
            { key: "affTau", label: t("panel.field.affTau"), value: String(config.affection_tau_days ?? "-") },
            { key: "grace", label: t("panel.field.grace"), value: String(config.grace_hours ?? "-") },
            {
              key: "rate",
              label: t("panel.field.rate"),
              value: t("panel.rateValue", {
                interval: Math.round(Number(config.min_interval_sec ?? 0) / 60),
                max: config.max_per_hour ?? "-",
              }),
            },
            { key: "store", label: t("panel.field.store"), value: state?.store_available ? "OK" : "-" },
          ]}
        />
      </Card>
    </Stack>
  )

  const tabItems = [
    { id: "overview", label: t("panel.tab.overview"), content: overviewTab },
    { id: "life", label: t("panel.tab.life"), content: lifeTab },
    { id: "her", label: t("panel.tab.her"), content: herTab },
    { id: "admin", label: t("panel.tab.admin"), content: adminTab },
  ]

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Stack gap={16}>
        {!enabled ? <Alert tone="warning" message={t("panel.frozen")} /> : null}
        {state?.error_code ? (
          <Alert tone="danger" message={t(`panel.errors.${camel(String(state.error_code))}`)} />
        ) : null}
        {advisor.urgent ? <Alert tone="danger" message={t("panel.advisor.urgent", { days: daysRemaining ?? 0 })} /> : null}

        {snapshot ? (
          <>
            {/* 顶部状态带：滚到哪个标签页都能一眼看到她现在的样子。 */}
            <Card title={t("panel.section.stats")}>
              <Stack gap={12}>
                <Inline gap={8} align="center" wrap>
                  <StatusBadge
                    tone="info"
                    label={t("panel.band.day", { day: runtime.day_number ?? snapshot.day_number ?? 0 })}
                  />
                  <StatusBadge tone="default" label={t("panel.band.streak", { streak: snapshot.streak_days ?? 0 })} />
                  <StatusBadge
                    tone="default"
                    label={t(`panel.phase.${runtime.phase ?? "noon"}`, { defaultValue: runtime.phase ?? "-" })}
                  />
                  {sleeping ? <StatusBadge tone="info" label={t("panel.band.sleeping")} /> : null}
                  <StatusBadge
                    tone={sleeping ? "info" : "default"}
                    label={
                      sleeping
                        ? t("panel.band.toWake", { minutes: runtime.minutes_to_boundary ?? 0 })
                        : t("panel.band.toSleep", { minutes: runtime.minutes_to_boundary ?? 0 })
                    }
                  />
                  {runtime.crisis && crisisAxes
                    ? <StatusBadge tone="warning" label={t("panel.band.crisis", { axes: crisisAxes })} />
                    : null}
                </Inline>
                {focusSwitcher}
                {/* 五轴用 fluid Columns：窄面板下自动流式换行（Grid cols=5 只有"5 列/1 列"两档）。
                    数值不写进 label：kit 的 Progress 自带右侧百分比，写了会出现两个数。 */}
                <Columns cols={5} minWidth={150} gap={12}>
                  {STAT_KEYS.map((key) => (
                    <Progress
                      key={key}
                      label={`${t(`panel.stat.${key}`)} · ${tierOf(key)}`}
                      value={Math.round(Number(snapshot[key] ?? 0))}
                    />
                  ))}
                </Columns>
                <Inline gap={16} align="center">
                  <Switch checked={enabled} label={t("panel.enabled")} onChange={(next: boolean) => toggleEnabled(next)} />
                  <Text>{enabled ? t("panel.running") : t("panel.stopped")}</Text>
                </Inline>
              </Stack>
            </Card>

            <Tabs id="our_life.main" items={tabItems} />
          </>
        ) : (
          <>
            {/* 无分片也要递得出总开关：v0.4.1 之前开关只存在于状态带里，
                而状态带只在有分片时渲染——新装机"先有鸡还是先有蛋"的死锁在这。 */}
            <Card title={t("panel.section.switch")}>
              <Stack gap={12}>
                <Inline gap={16} align="center" wrap>
                  <Switch checked={enabled} label={t("panel.enabled")} onChange={(next: boolean) => toggleEnabled(next)} />
                  <Text>{enabled ? t("panel.running") : t("panel.stopped")}</Text>
                </Inline>
                {focusSwitcher}
                <EmptyState title={t("panel.noShard")} description={t("panel.noShardHint")} />
              </Stack>
            </Card>
          </>
        )}
      </Stack>
    </Page>
  )
}
