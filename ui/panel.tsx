// Hosted TSX 面板：只从 `@neko/plugin-ui` 导入，业务逻辑全在 Python 侧。
//
// 契约要点（照 `plugin/sdk/hosted-ui/index.d.ts` 的精确签名写，不照文档猜）：
// - `Grid` 的列数是 `cols`（不是 columns）；`Select` / `NumberInput` **没有** `label`，要包 `Field`；
//   `StatusBadge` 用 `label`（不是 text）；`Tone` 只有 primary/success/warning/danger/info/default。
// - 动作调用返回的是**信封** `{plugin_id, action_id, result}`，真正的返回值在 `.result`。
// - 只允许相对导入与 `@neko/plugin-ui`；必须 `export default` 一个函数组件。
// - 检查器是**文本级**规则：文件里出现"全局门面对象"的裸标识符形态就会被拒收，
//   所以一律写完整的 `props.api` 成员访问（连注释也不例外）。
import {
  ActionButton,
  Alert,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Inline,
  KeyValue,
  NumberInput,
  Page,
  Progress,
  Select,
  Stack,
  StatCard,
  Switch,
  Text,
  useConfirm,
  useLocalState,
  useToast,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

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
}

type ShopEntry = {
  id?: string
  kind?: string
  cost?: number
  food?: boolean
  carry_max?: number
  effects?: [string, number][]
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
  trend?: InjectionRecord[]
  hours?: number[]
  meal_days?: [string, number][]
  error_code?: string
}

const STAT_KEYS = ["energy", "satiety", "mood", "health", "affection"] as const
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
  const { t, state, actions } = props
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
  const enabled = state?.enabled === true
  const config = state?.config ?? {}
  const tiers = snapshot?.tiers ?? {}
  const inventory = snapshot?.inventory ?? {}
  const catalog = state?.shop ?? []

  const actionOf = (id: string): HostedAction | undefined =>
    actions.find((action) => action.id === id || action.entry_id === id)

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

  const effectText = (entry: ShopEntry): string =>
    (entry.effects ?? [])
      .map(([name, delta]) => `${t(`panel.stat.${name}`, { defaultValue: name })} ${delta > 0 ? "+" : ""}${delta}`)
      .join(" · ")

  const daysRemaining = advisor.days_remaining
  const advisorLine =
    daysRemaining === null || daysRemaining === undefined
      ? t("panel.advisor.stock", { units: advisor.stock_meals ?? 0 })
      : t("panel.advisor.days", { days: daysRemaining })

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Stack gap={16}>
        {!enabled ? <Alert tone="warning" message={t("panel.frozen")} /> : null}
        {state?.error_code ? (
          <Alert tone="danger" message={t(`panel.errors.${camel(String(state.error_code))}`)} />
        ) : null}
        {advisor.urgent ? <Alert tone="danger" message={t("panel.advisor.urgent", { days: daysRemaining ?? 0 })} /> : null}

        <Card title={t("panel.section.switch")}>
          <Inline gap={16} align="center">
            <Switch checked={enabled} label={t("panel.enabled")} onChange={(next: boolean) => toggleEnabled(next)} />
            <Text>{enabled ? t("panel.running") : t("panel.stopped")}</Text>
            {actionOf("status") ? (
              <ActionButton
                action={actionOf("status")}
                label={t("actions.status.label")}
                onResult={() => toast.success(t("panel.msg.statsLoaded"))}
                onError={(error: Error) => toast.error(errorText(error, t))}
              />
            ) : null}
          </Inline>
        </Card>

        {snapshot ? (
          <>
            <Grid cols={5} gap={12}>
              {STAT_KEYS.map((key) => (
                <StatCard
                  key={key}
                  label={t(`panel.stat.${key}`)}
                  value={String(t(`panel.tier.${key}.${tiers[key] ?? "unknown"}`, { defaultValue: tiers[key] ?? "-" }))}
                />
              ))}
            </Grid>

            <Card title={t("panel.section.stats")}>
              <Stack gap={12}>
                {STAT_KEYS.map((key) => {
                  const value = Number(snapshot[key] ?? 0)
                  return (
                    <Progress
                      key={key}
                      label={`${t(`panel.stat.${key}`)} · ${t(`panel.tier.${key}.${tiers[key] ?? "unknown"}`, {
                        defaultValue: tiers[key] ?? "-",
                      })} · ${value.toFixed(1)}`}
                      value={value}
                    />
                  )
                })}
              </Stack>
            </Card>

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

            <Card title={t("panel.section.advisor")}>
              <Stack gap={12}>
                <KeyValue
                  items={[
                    { key: "sodas", label: t("panel.field.sodas"), value: String(snapshot.sodas ?? 0) },
                    { key: "spent", label: t("panel.field.dailySpent"), value: String(snapshot.daily_spent ?? 0) },
                    { key: "need", label: t("panel.field.economy"), value: t("panel.advisor.need", { meals: advisor.meals_per_day ?? 0 }) },
                    { key: "stock", label: t("panel.field.staple"), value: advisorLine },
                    { key: "mealsToday", label: t("panel.field.mealsToday"), value: String(snapshot.meals_today ?? 0) },
                    { key: "mealsTotal", label: t("panel.field.mealsTotal"), value: String(snapshot.meals_total ?? 0) },
                    { key: "lastMeal", label: t("panel.field.lastMeal"), value: formatTime(snapshot.last_meal_at, t("panel.never")) },
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

            <Card title={t("panel.section.shop")}>
              <Stack gap={12}>
                <Grid cols={2} gap={12}>
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
                </Grid>
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

            <Card title={t("panel.section.rhythm")}>
              <Stack gap={12}>
                <KeyValue
                  items={[
                    { key: "lanlan", label: t("panel.field.lanlan"), value: snapshot.lanlan ?? "-" },
                    {
                      key: "phase",
                      label: t("panel.field.phase"),
                      value: t(`panel.phase.${runtime.phase ?? "noon"}`, { defaultValue: runtime.phase ?? "-" }),
                    },
                    {
                      key: "boundary",
                      label: runtime.sleeping ? t("panel.field.boundaryWake") : t("panel.field.boundarySleep"),
                      value: String(runtime.minutes_to_boundary ?? 0),
                    },
                    { key: "dayNumber", label: t("panel.field.dayNumber"), value: String(runtime.day_number ?? 0) },
                    { key: "streak", label: t("panel.field.streak"), value: String(snapshot.streak_days ?? 0) },
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
                      value: formatTime(snapshot.last_touch_at, t("panel.never")),
                    },
                    { key: "gap", label: t("panel.field.gap"), value: formatGap(snapshot.gap_hours, t) },
                    {
                      key: "injects",
                      label: t("panel.field.inject24h"),
                      value: String(snapshot.inject_count_24h ?? 0),
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

            <Card title={t("panel.section.tune")}>
              <Stack gap={12}>
                <Field label={t("panel.field.stat")} help={t("panel.tuneHelp")}>
                  <Select value={tuneStat} options={statOptions} onChange={(next: any) => setTuneStat(String(next))} />
                </Field>
                <Field label={t("panel.field.value")}>
                  <NumberInput
                    value={tuneValue}
                    min={0}
                    max={100}
                    step={1}
                    onChange={(next: number | string) => setTuneValue(next === "" ? "" : Number(next))}
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
          </>
        ) : (
          <EmptyState title={t("panel.noShard")} description={t("panel.noShardHint")} />
        )}
      </Stack>
    </Page>
  )
}
