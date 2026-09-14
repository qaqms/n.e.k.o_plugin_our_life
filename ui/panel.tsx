// Hosted TSX 面板：只从 `@neko/plugin-ui` 导入，业务逻辑全在 Python 侧。
//
// 契约要点（照 `plugin/sdk/hosted-ui/index.d.ts` 的精确签名写，不照文档猜）：
// - `Grid` 的列数是 `cols`（不是 columns）；`Select` / `NumberInput` **没有** `label`，要包 `Field`；
//   `StatusBadge` 用 `label`（不是 text）；`Tone` 只有 primary/success/warning/danger/info/default。
// - 动作调用返回的是**信封** `{plugin_id, action_id, result}`，真正的返回值在 `.result`。
// - 只允许相对导入与 `@neko/plugin-ui`；必须 `export default` 一个函数组件。
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
  tiers?: Record<string, string>
  streak_days?: number
  last_touch_at?: number | null
  gap_hours?: number | null
  last_inject_at?: number | null
  inject_count_24h?: number
}

type InjectionRecord = {
  at?: number
  trigger?: string
  summary?: string
}

type State = {
  enabled?: boolean
  lanlan?: string
  shards?: string[]
  store_available?: boolean
  config?: Record<string, any>
  state?: ShardSnapshot | null
  recent_injections?: InjectionRecord[]
  hours?: number[]
  error_code?: string
}

const STAT_KEYS = ["affection", "mood", "health"] as const

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

  const snapshot = state?.state ?? null
  const enabled = state?.enabled === true
  const config = state?.config ?? {}
  const tiers = snapshot?.tiers ?? {}

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
    if (result?.note === "stat_updated") {
      toast.success(t("panel.msg.statUpdated"))
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

  const hours = state?.hours ?? []
  const activeHours = hours
    .map((count, hour) => ({ count, hour }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count)
    .slice(0, 6)

  const injections = state?.recent_injections ?? []

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Stack gap={16}>
        {!enabled ? <Alert tone="warning" message={t("panel.frozen")} /> : null}
        {state?.error_code ? (
          <Alert tone="danger" message={t(`panel.errors.${camel(String(state.error_code))}`)} />
        ) : null}

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
            <Grid cols={3} gap={12}>
              {STAT_KEYS.map((key) => (
                <StatCard
                  key={key}
                  label={t(`panel.stat.${key}`)}
                  value={`${t(`panel.tier.${key}.${tiers[key] ?? "unknown"}`, {
                    defaultValue: tiers[key] ?? "-",
                  })}`}
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

            <Card title={t("panel.section.rhythm")}>
              <Stack gap={12}>
                <KeyValue
                  items={[
                    { key: "lanlan", label: t("panel.field.lanlan"), value: snapshot.lanlan ?? "-" },
                    { key: "streak", label: t("panel.field.streak"), value: String(snapshot.streak_days ?? 0) },
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
