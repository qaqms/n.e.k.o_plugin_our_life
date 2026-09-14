// Hosted TSX 面板骨架：只从 `@neko/plugin-ui` 与 `./shared` / `./components/**` 导入，
// 业务逻辑全在 Python 侧。
//
// 契约要点（照 `plugin/sdk/hosted-ui/index.d.ts` 的精确签名写，不照文档猜）：
// - `Grid` 的列数是 `cols`（不是 columns）；`Select` / `NumberInput` **没有** `label`，要包 `Field`；
//   `StatusBadge` 用 `label`（不是 text）；`Tone` 只有 primary/success/warning/danger/info/default。
// - 动作调用返回的是**信封** `{plugin_id, action_id, result}`，真正的返回值在 `.result`。
// - 只允许相对导入与 `@neko/plugin-ui`；必须 `export default` 一个函数组件。
// - 检查器是**文本级**规则：文件里出现"全局门面对象"的裸标识符形态就会被拒收，
//   所以一律写完整的 `props.api` 成员访问（连注释也不例外）。
//
// 布局（v0.4.1 制结构 → v0.6.0 内容细化）：
// - **顶部状态带**（v0.4.1，保留）：五轴进度条 + 今日事实徽章 + 总开关，滚到哪都可见；
// - **Tabs 四页**：总览（今日带 + 五轴卡 + 作息条）/ 过日子（口粮顾问 + 商店 + 背包卡）/
//   她的世界（判断 + 经历时间线）/ 管理（纠偏 + 近期注入 + 配置）。
// - **文件拆分（v0.6.0）**：可视模块拆到 `ui/components/**`，类型与纯函数在 `ui/shared.tsx`；
//   本文件只留骨架、动作通道（run/refreshContext）与状态带。
// - **自动刷新（v0.4.3）+ 动作后即时刷新（v0.4.4）**：每 10s 单飞合并拉一次 context；
//   kit 只对 ActionButton/ActionForm 兑现 refresh_context，本面板全走普通 Button——
//   run() 成功后自己调 refreshContext（与轮询共用单飞通道）。
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
import {
  STAT_KEYS,
  camel,
  envelopeResult,
  errorText,
  formatTime,
} from "./shared"
import type { InjectionRecord, JudgmentRecord, PanelProps, ShopEntry } from "./shared"
import { AxisCards } from "./components/axis_cards"
import { Bag } from "./components/bag"
import { DayBand } from "./components/day_band"
import { RhythmBar } from "./components/rhythm_bar"
import { Timeline } from "./components/timeline"

// 禁用态光标覆盖（v0.4.5）+ 作息条格样式（v0.6.0）：kit 的 `.neko-button:disabled` 是
// `cursor: wait`（转圈"等待"），而我们的禁用只是"总开关没开、按不了"。平台层 CSS 只读，
// 这里用面板级 <style> 覆盖：同特异度后来者赢，kit 样式表必然先于面板内联样式入文档。
const PANEL_STYLE_OVERRIDES =
  ".neko-button:disabled { cursor: not-allowed; }" +
  " .our-life-hour { display: inline-block; min-width: 13px; text-align: center; border-radius: 3px; }" +
  " .our-life-hour-sleep { background: rgba(125, 125, 160, 0.28); }" +
  " .our-life-hour-now { outline: 1px solid currentColor; }"

// 自动轮询节奏（v0.4.3）：见 Panel 内「自动刷新」注释段的三条性能闸门论证。
const AUTO_REFRESH_MS = 10000

export default function Panel(props: PanelProps) {
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

  const snapshot = state?.state ?? null
  const runtime = state?.runtime ?? {}
  const advisor = runtime.advisor ?? {}
  const feedback: any = runtime.feedback ?? {}
  const judgmentHistory = feedback.history ?? []
  const eventHistory = state?.recent_events ?? []
  const enabled = state?.enabled === true
  const config = state?.config ?? {}
  const tiers = snapshot?.tiers ?? {}
  const inventory = snapshot?.inventory ?? {}
  const catalog = state?.shop ?? []
  const axes = state?.axes ?? {}
  const sleeping = runtime.sleeping ?? snapshot?.sleeping ?? false

  // context 拉取的唯一通道（v0.4.4）：轮询与动作后的即时刷新共用，单飞 + 尾随合并。
  // v0.4.3 的教训：`refresh_context=True` 只有 kit 的 ActionButton/ActionForm 会自动消费
  // （宿主 ui-kit/runtime.js），本面板全部走普通 Button —— 动作成功后没人重拉 context，
  // 金币/背包就会「后台已扣、前台不更新」。
  // 合并而非排队：同一时刻最多一个在途请求 + 一次尾随补拉，慢机器也不叠请求。
  const refreshBusy = useRef(false)
  const refreshRerun = useRef(false)
  const refreshContext = async () => {
    if (refreshBusy.current) {
      refreshRerun.current = true
      return
    }
    refreshBusy.current = true
    try {
      do {
        refreshRerun.current = false
        await props.api.refresh()
      } while (refreshRerun.current)
    } catch {
      // 刷新失败静默：动作本身已成功、数据已落库，轮询会在下一拍补上。
    } finally {
      refreshBusy.current = false
      refreshRerun.current = false
    }
  }

  const run = async (actionId: string, args: Record<string, any>) => {
    try {
      const envelope = await props.api.call(actionId, args, { timeoutMs: 20000 })
      const result = envelopeResult(envelope)
      // 成功（含 store_unavailable 降级码）才拉：Err 会以异常走下面的 catch，不该动 context。
      if (result) await refreshContext()
      return result
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

  // 背包卡的「给她」直达按钮（v0.6.0）：旧版"下拉选中→再按照顾"的两步流砍成一步。
  const giveItem = async (itemId: string) => {
    const result = await run("feed", { item: itemId })
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
  }

  // 自动刷新（v0.4.3）：手动「刷新」按钮退役——面板数值是后端 tick 结算后、
  // 读取时刻按真实时间折算的结果，让用户按按钮去拉快照等于让用户充当定时器。
  // 三条性能闸门：
  // - 单飞合并：refreshContext 内部保证同一时刻最多一个在途 + 一次尾随补拉；
  // - 后台暂停：document.hidden（面板不可见）时不拉数据；
  // - 回可见补拉：从后台切回来立刻补一轮，再恢复常态节奏。
  // 10s 与 tick_seconds=30 的结算周期同量级：面板最多滞后 10 秒；她「主动开口」
  // 走 push 通道、不依赖这里，轮询只服务面板观察。动作后的即时刷新走 run()
  // 成功路径，与这里共用 refreshContext——轮询只是「没有人操作时数据也不旧」的兜底。
  useEffect(() => {
    const hidden = () => typeof document !== "undefined" && document.hidden === true
    const tick = async () => {
      if (hidden()) return
      await refreshContext()
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

  const hours = state?.hours ?? []
  const injections = state?.recent_injections ?? []
  const trend = state?.trend ?? []
  const trendPoints = (trend.length > 0 ? trend : injections.filter((row) => row.stats)).slice(-12)

  // 每轴走势序列（v0.6.0）：走势卡被五轴卡吸收后，sparkline 数据按轴分发。
  const sparks: Record<string, number[]> = {}
  for (const key of STAT_KEYS) {
    sparks[key] = trendPoints.map((row) => Number(row.stats?.[key] ?? 0))
  }
  const axisValues: Record<string, number> = {}
  for (const key of STAT_KEYS) {
    axisValues[key] = Number(snapshot?.[key] ?? 0)
  }

  const crisisAxes = (runtime.crisis_axes ?? [])
    .map((axis) => t(`panel.stat.${axis}`, { defaultValue: axis }))
    .join(" · ")

  const effectText = (entry: ShopEntry): string =>
    (entry.effects ?? [])
      .map(([name, delta]) => `${t(`panel.stat.${name}`, { defaultValue: name })} ${delta > 0 ? "+" : ""}${delta}`)
      .join(" · ")

  // 背包卡的呈现数据（v0.6.0）：只有"在她手上"的物品进卡，顺序跟商店目录一致。
  const bagItems = catalog
    .filter((entry) => (inventory[String(entry.id ?? "")] ?? 0) > 0)
    .map((entry) => ({
      id: String(entry.id ?? ""),
      label: t(`panel.item.${entry.id ?? "unknown"}`, { defaultValue: String(entry.id ?? "-") }),
      count: inventory[String(entry.id ?? "")] ?? 0,
      effects: t("panel.shopEffects", { effects: effectText(entry) }),
    }))

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
      <DayBand t={t} snapshot={snapshot} dayNumber={runtime.day_number} />
      <AxisCards t={t} tiers={tiers} values={axisValues} axes={axes} sparks={sparks} />
      <RhythmBar t={t} hours={hours} sleeping={sleeping} config={config} runtime={runtime} snapshot={snapshot} />
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
              { key: "spent", label: t("panel.field.dailySpent"), value: String(snapshot?.daily_spent ?? 0) },
              {
                key: "need",
                label: t("panel.field.economy"),
                value: t("panel.advisor.need", { meals: advisor.meals_per_day ?? 0 }),
              },
              { key: "stock", label: t("panel.field.staple"), value: advisorLine },
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

        <Bag t={t} items={bagItems} enabled={enabled} onGive={giveItem} />
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

      <Timeline t={t} events={eventHistory} total={snapshot?.events?.total ?? eventHistory.length} />
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
        <style>{PANEL_STYLE_OVERRIDES}</style>
        {!enabled ? <Alert tone="warning" message={t("panel.frozen")} /> : null}
        {state?.error_code ? (
          <Alert tone="danger" message={t(`panel.errors.${camel(String(state.error_code))}`)} />
        ) : null}
        {advisor.urgent ? <Alert tone="danger" message={t("panel.advisor.urgent", { days: daysRemaining ?? 0 })} /> : null}

        {snapshot ? (
          <>
            {/* 顶部状态带：滚到哪个标签页都能一眼看到她现在的样子（v0.4.1 真机成果，保留）。 */}
            <Card title={t("panel.band.title")}>
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
