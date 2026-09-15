// 签到日历（v0.7.0）：月视图网格——UI kit 没有 Calendar，与 24 格作息条同一条
// "自绘"纪律：只用布局原语 + 面板级 CSS 类，不引任何外部组件。
//
// 分工：判据全在后端（`core/checkin.py`）——连续天数、能否补签、发多少钱，
// 这里只把 `{log, makeups, today}` 画成格子并收集点击。可补日候选在本地算
// （窗内 + 未签），但真正裁决在 `makeup` 入口：**这里永远不显示"能拿到多少钱"**，
// 只照抄后端给的 `next_reward` / `makeup_cost`。
//
// 星期从周一开始（中文语境习惯）；月切换只动本地游标 `monthCursor`
// （useLocalState 活过 context 刷新），不碰任何业务状态。

import {
  Button,
  Card,
  Columns,
  Field,
  Grid,
  Inline,
  Select,
  Stack,
  StatCard,
  StatusBadge,
  Text,
  useConfirm,
  useLocalState,
} from "@neko/plugin-ui"
import type { CheckinLogEntry, CheckinView, Translate } from "../shared"

const WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

function pad2(value: number): string {
  return String(value).padStart(2, "0")
}

/** "YYYY-MM" ± N 个月（跨年自动进/退位）。 */
function shiftMonth(month: string, delta: number): string {
  const parts = month.split("-").map(Number)
  if (parts.length !== 2 || parts.some((part) => !Number.isFinite(part))) return month
  const index = parts[0] * 12 + (parts[1] - 1) + delta
  return `${String(Math.floor(index / 12)).padStart(4, "0")}-${pad2((index % 12) + 1)}`
}

/** 本地时区的 ISO 日期串：用 UTC 正午避免夏令时把日期掰动一天。 */
function isoFromUtcNoon(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

function dayOffsetIso(today: string, offsetDays: number): string {
  const base = Date.parse(`${today}T12:00:00Z`)
  if (Number.isNaN(base)) return ""
  return isoFromUtcNoon(base + offsetDays * 86400000)
}

type DayCell = {
  iso: string
  day: number
  state: string
  coins: number
  blank: boolean
}

export function CheckinCalendar(props: {
  t: Translate
  checkin: CheckinView
  masterEnabled: boolean
  onCheckin: () => Promise<void>
  onMakeup: (day: string) => Promise<void>
}) {
  const t = props.t
  const checkin = props.checkin ?? {}
  const today = String(checkin.today ?? "")
  const confirm = useConfirm()
  const log = checkin.log ?? []
  const makeups = checkin.makeups ?? []

  // 日历三张表：签到金额 / 幸运标记 / 补签标记。lucky 覆盖 checked 的记号。
  const coinsByDay: Record<string, number> = {}
  const luckyDays: Record<string, boolean> = {}
  for (const entry of log) {
    const row = entry as CheckinLogEntry
    coinsByDay[String(row[0])] = Number(row[1]) ?? 0
    luckyDays[String(row[0])] = row[2] === true
  }
  const makeupDays: Record<string, boolean> = {}
  for (const day of makeups) makeupDays[String(day)] = true
  const checkedDays: Record<string, boolean> = {}
  for (const day of Object.keys(coinsByDay)) checkedDays[day] = true
  for (const day of makeups) checkedDays[String(day)] = true
  for (const day of makeups) checkedDays[String(day)] = true

  // 月游标：空串 = 跟随今天的月份；翻页只改本地状态（useLocalState 活过刷新）。
  const currentMonth = today ? today.slice(0, 7) : ""
  const [monthCursor, setMonthCursor] = useLocalState<string>("our_life.checkin.month", "")
  const month = monthCursor || currentMonth

  const cells: DayCell[] = []
  if (month && currentMonth) {
    const first = Date.parse(`${month}-01T12:00:00Z`)
    const year = Number(month.slice(0, 4))
    const monthNo = Number(month.slice(5, 7))
    const totalDays = new Date(Date.UTC(year, monthNo, 0)).getUTCDate()
    // 周一开头：getUTCDay 0=周日 → 偏移 (day+6)%7
    const leading = ((new Date(first).getUTCDay() + 6) % 7)
    for (let index = 0; index < leading; index += 1) {
      cells.push({ iso: "", day: 0, state: "blank", coins: 0, blank: true })
    }
    for (let day = 1; day <= totalDays; day += 1) {
      const iso = `${month}-${pad2(day)}`
      let state = "missed"
      if (iso === today) state = checkedDays[iso] ? "today_done" : "today_open"
      else if (iso > today) state = "future"
      else if (luckyDays[iso]) state = "lucky"
      else if (makeupDays[iso]) state = "madeup"
      else if (checkedDays[iso]) state = "checked"
      cells.push({ iso, day, state, coins: coinsByDay[iso] ?? 0, blank: false })
    }
  }

  // 补签候选：过去 `window` 天内、没签过的日子（裁决仍在后端，这里只圈范围）。
  const windowDays = Number(checkin.makeup_window_days ?? 0)
  const makeupLeft = Number(checkin.makeup_left ?? 0)
  const makeupOptions: { value: string; label: string }[] = []
  for (let offset = 1; offset <= windowDays; offset += 1) {
    const iso = dayOffsetIso(today, -offset)
    if (iso && !checkedDays[iso]) {
      makeupOptions.push({ value: iso, label: iso })
    }
  }
  const [makeupDay, setMakeupDay] = useLocalState<string>("our_life.checkin.makeup_day", "")
  const pickedMakeup = makeupOptions.some((option) => option.value === makeupDay) ? makeupDay : ""

  const canCheckin = props.masterEnabled && checkin.feature_enabled === true && checkin.checked_today !== true
  const canMakeup =
    props.masterEnabled &&
    checkin.feature_enabled === true &&
    makeupLeft > 0 &&
    Boolean(pickedMakeup)

  const markOf = (cell: DayCell): string => {
    if (cell.blank) return ""
    if (cell.state === "lucky") return "⭐"
    if (cell.state === "checked" || cell.state === "today_done") return "✓"
    if (cell.state === "madeup") return "◐"
    if (cell.state === "missed") return "✗"
    return ""
  }
  const classOf = (cell: DayCell): string => {
    if (cell.blank) return "our-life-cal-cell our-life-cal-blank"
    const parts = ["our-life-cal-cell"]
    if (cell.state === "checked" || cell.state === "today_done") parts.push("our-life-cal-checked")
    if (cell.state === "madeup") parts.push("our-life-cal-madeup")
    if (cell.state === "lucky") parts.push("our-life-cal-checked", "our-life-cal-lucky")
    if (cell.state === "missed") parts.push("our-life-cal-missed")
    if (cell.state === "future") parts.push("our-life-cal-future")
    if (cell.state === "today_open" || cell.state === "today_done") parts.push("our-life-cal-today")
    return parts.join(" ")
  }

  return (
    <Card title={t("panel.cal.title")}>
      <Stack gap={12}>
        <Columns cols={3} minWidth={120} gap={12}>
          <StatCard label={t("panel.cal.streak")} value={String(checkin.streak ?? 0)} />
          <StatCard label={t("panel.cal.best")} value={String(checkin.best ?? 0)} />
          <StatCard label={t("panel.cal.makeupLeft")} value={String(makeupLeft)} />
        </Columns>
        <Inline gap={8} align="center">
          <Button
            tone="default"
            onClick={async () => {
              setMonthCursor(shiftMonth(month, -1))
            }}
          >
            {t("panel.cal.prev")}
          </Button>
          <Text>{month}</Text>
          <Button
            tone="default"
            disabled={month >= currentMonth}
            onClick={async () => {
              setMonthCursor(shiftMonth(month, 1))
            }}
          >
            {t("panel.cal.next")}
          </Button>
          <StatusBadge
            tone={checkin.checked_today ? "success" : "default"}
            label={
              checkin.checked_today
                ? t("panel.cal.checkedToday")
                : t("panel.cal.notYet", { coins: checkin.next_reward ?? 0 })
            }
          />
        </Inline>
        <Grid cols={7} gap={4}>
          {WEEKDAY_KEYS.map((key) => (
            <Text key={`w-${key}`} className="our-life-cal-weekday">
              {t(`panel.cal.w-${key}`)}
            </Text>
          ))}
          {cells.map((cell, index) => (
            <Text key={cell.blank ? `b-${index}` : cell.iso} className={classOf(cell)}>
              {cell.blank ? "" : `${cell.day} ${markOf(cell)}`.trim()}
            </Text>
          ))}
        </Grid>
        <Text>{t("panel.cal.legend")}</Text>
        <Inline gap={12} align="center" wrap>
          <Button tone="primary" disabled={!canCheckin} onClick={props.onCheckin}>
            {t("actions.checkin.label")}
          </Button>
          {makeupOptions.length > 0 ? (
            <Field label={t("panel.cal.pickDay")}>
              <Select
                value={pickedMakeup}
                options={makeupOptions}
                onChange={(next: any) => setMakeupDay(String(next))}
              />
            </Field>
          ) : (
            <Text>{t("panel.cal.makeupNone")}</Text>
          )}
          <Button
            tone="warning"
            disabled={!canMakeup}
            onClick={async () => {
              if (!pickedMakeup) return
              const ok = await confirm({
                title: t("panel.makeupConfirm.title"),
                message: t("panel.makeupConfirm.message", {
                  day: pickedMakeup,
                  cost: checkin.makeup_cost ?? 0,
                }),
                tone: "warning",
                confirmLabel: t("panel.makeupConfirm.confirm"),
                cancelLabel: t("panel.makeupConfirm.cancel"),
              })
              if (!ok) return
              await props.onMakeup(pickedMakeup)
            }}
          >
            {t("actions.makeup.label")}
          </Button>
        </Inline>
      </Stack>
    </Card>
  )
}
