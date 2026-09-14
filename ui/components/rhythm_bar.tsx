// 她的一天（v0.6.0）：24 格作息条——柱高=该小时的互动计数（相对当日峰值），
// 睡眠窗打底色，当前小时描边。旧版把这一切压成 KeyValue 里的"距入睡 231"一行字。
//
// 呈现层只画图：hours / sleep_start_hour / sleep_end_hour 都来自 context，
// 睡眠窗跨零点（如 23→7）在这里处理，因为这是**画法**问题而不是业务判据
// （真判据在 core/rhythm.resolve_rhythm，面板不参与"她睡没睡"的裁决）。

import { Card, Inline, KeyValue, Stack, Text } from "@neko/plugin-ui"
import { formatGap, formatTime, hourBars } from "../shared"
import type { RuntimeView, ShardSnapshot, Translate } from "../shared"

function isSleepHour(hour: number, start: number, end: number): boolean {
  if (start === end) return false
  if (start < end) return hour >= start && hour < end
  return hour >= start || hour < end
}

export function RhythmBar(props: {
  t: Translate
  hours: number[]
  sleeping: boolean
  config: Record<string, any>
  runtime: RuntimeView
  snapshot: ShardSnapshot
}) {
  const t = props.t
  const config = props.config ?? {}
  const start = Number(config.sleep_start_hour ?? 0)
  const end = Number(config.sleep_end_hour ?? 0)
  const counts = props.hours && props.hours.length === 24 ? props.hours.map((count) => Number(count) || 0) : []
  const bars = counts.length === 24 ? hourBars(counts) : []
  const nowHour = new Date().getHours()
  const activeHours = counts
    .map((count, hour) => ({ count, hour }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count)
    .slice(0, 6)
  const anniversary = props.runtime.anniversary
  return (
    <Card title={t("panel.rhythm.title")}>
      <Stack gap={12}>
        {bars.length === 24 ? (
          <Stack gap={4}>
            <Inline gap={2}>
              {bars.map((bar, hour) => (
                <Text
                  key={hour}
                  className={`our-life-hour${isSleepHour(hour, start, end) ? " our-life-hour-sleep" : ""}${hour === nowHour ? " our-life-hour-now" : ""}`}
                >
                  {bar}
                </Text>
              ))}
            </Inline>
            <Text>{t("panel.rhythm.scale")}</Text>
          </Stack>
        ) : null}
        <Text>
          {t("panel.rhythm.legend", {
            sleep: `${String(start).padStart(2, "0")}:00 → ${String(end).padStart(2, "0")}:00`,
            phase: t(`panel.phase.${props.runtime.phase ?? "noon"}`, { defaultValue: props.runtime.phase ?? "-" }),
            boundary: props.sleeping
              ? t("panel.band.toWake", { minutes: props.runtime.minutes_to_boundary ?? 0 })
              : t("panel.band.toSleep", { minutes: props.runtime.minutes_to_boundary ?? 0 }),
          })}
        </Text>
        {activeHours.length > 0 ? (
          <Inline gap={8} wrap>
            <Text>{t("panel.rhythm.active")}</Text>
            {activeHours.map((item) => (
              <Text key={item.hour}>{`${String(item.hour).padStart(2, "0")}:00 ×${item.count}`}</Text>
            ))}
          </Inline>
        ) : (
          <Text>{t("panel.hoursHint")}</Text>
        )}
        <KeyValue
          items={[
            {
              key: "anniversary",
              label: t("panel.field.anniversary"),
              value: anniversary
                ? anniversary.kind === "yearly"
                  ? t("panel.anniversary.yearly", { years: anniversary.years ?? 1 })
                  : t("panel.anniversary.milestone", { day: anniversary.day_number ?? 0 })
                : "-",
            },
            {
              key: "last",
              label: t("panel.field.lastTouch"),
              value: formatTime(props.snapshot.last_touch_at, t("panel.never")),
            },
            { key: "gap", label: t("panel.field.gap"), value: formatGap(props.snapshot.gap_hours, t) },
            {
              key: "decay",
              label: t("panel.field.perDayDecay"),
              value: String(props.runtime.per_day_decay ?? "-"),
            },
          ]}
        />
      </Stack>
    </Card>
  )
}
