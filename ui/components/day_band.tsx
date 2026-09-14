// 今日事实带（v0.6.0）：把散在状态带徽章与KeyValue文字行里的"今天的事实"
// 提出来立成五个读数——"陪到第几天"这类数字不该要用户在九行列表里找。

import { Card, Columns, StatCard } from "@neko/plugin-ui"
import type { ShardSnapshot, Translate } from "../shared"

export function DayBand(props: { t: Translate; snapshot: ShardSnapshot; dayNumber: number | undefined }) {
  const t = props.t
  const snapshot = props.snapshot
  return (
    <Card title={t("panel.today.title")}>
      <Columns cols={5} minWidth={120} gap={12}>
        <StatCard label={t("panel.today.day")} value={String(props.dayNumber ?? snapshot.day_number ?? 0)} />
        <StatCard label={t("panel.today.streak")} value={String(snapshot.streak_days ?? 0)} />
        <StatCard label={t("panel.today.meals")} value={String(snapshot.meals_today ?? 0)} />
        <StatCard label={t("panel.today.spent")} value={String(snapshot.daily_spent ?? 0)} />
        <StatCard label={t("panel.today.spoke")} value={String(snapshot.inject_count_24h ?? 0)} />
      </Columns>
    </Card>
  )
}
