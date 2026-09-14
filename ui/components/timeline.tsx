// 她经历过什么（v0.6.0 时间线化）：DataTable 的三列表格读起来像后台日志，
// 换成"新的在前"的一列圆点——事件名、时刻、以及**当时数值跨线的幅度**（value ±width）。
// "病好了 · 健康 43（跨线 +3）"才是经历；"9/13 21:14 / 病好了 / 健康"是台账。
// 台账里没有正文（core/events.py 的隐私纪律），这里也就绝无可能出现。

import { Card, EmptyState, Inline, Stack, StatusBadge, Text } from "@neko/plugin-ui"
import { formatTime } from "../shared"
import type { EventRecord, Translate } from "../shared"

export function Timeline(props: { t: Translate; events: EventRecord[]; total: number }) {
  const t = props.t
  return (
    <Card title={t("panel.section.events")}>
      {props.events.length > 0 ? (
        <Stack gap={8}>
          {props.events.map((entry, index) => (
            <Inline key={`${String(entry.key)}:${String(entry.at)}`} gap={8} align="center" wrap>
              <Text>{index === 0 ? "●" : "○"}</Text>
              <Text>{formatTime(entry.at, "-")}</Text>
              <StatusBadge
                tone="success"
                label={t(`panel.event.${entry.key ?? "unknown"}`, { defaultValue: String(entry.key ?? "-") })}
              />
              <Text>
                {t("panel.timeline.snapshot", {
                  stat: t(`panel.stat.${entry.stat ?? "unknown"}`, { defaultValue: String(entry.stat ?? "-") }),
                  value: entry.value ?? "-",
                  width: entry.width ?? "-",
                })}
              </Text>
            </Inline>
          ))}
          {props.total > props.events.length ? (
            <Text>{t("panel.timeline.more", { shown: props.events.length, total: props.total })}</Text>
          ) : null}
          <Text>{t("panel.eventsHint")}</Text>
        </Stack>
      ) : (
        <EmptyState title={t("panel.noEvents")} description={t("panel.noEventsHint")} />
      )}
    </Card>
  )
}
