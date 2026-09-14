// 五轴卡（v0.6.0）：一条轴的"现在"聚成一张卡——数值、档位、走势、距升档、今日变化。
//
// 与旧"近期走势"卡的关系是**吸收**不是删除：sparkline 从五行大字报降为每卡一行；
// "距下一档还差多少 / 今日变化"来自 context 的 axes 字段（core.model.axis_details
// 现算，档位线唯一来源 TIER_BOUNDS）——面板自己不做任何档位运算。
// axes 缺席（旧 context / 无分片）时五卡照常渲染，只是少了明细行：呈现层容错，
// 数值容错不发生在这一层。

import { Card, Columns, Inline, Progress, Stack, StatusBadge, Text } from "@neko/plugin-ui"
import { STAT_KEYS, sparkline, tierTone } from "../shared"
import type { AxisView, Translate } from "../shared"

export function AxisCards(props: {
  t: Translate
  tiers: Record<string, string>
  values: Record<string, number>
  axes: Record<string, AxisView>
  sparks: Record<string, number[]>
}) {
  const t = props.t
  const axes = props.axes ?? {}
  return (
    <Card title={t("panel.section.stats")}>
      <Columns cols={5} minWidth={170} gap={12}>
        {STAT_KEYS.map((key) => {
          const axis = axes[key] ?? {}
          const tierName = t(`panel.tier.${key}.${axis.tier ?? props.tiers[key] ?? "unknown"}`, {
            defaultValue: axis.tier ?? props.tiers[key] ?? "-",
          })
          const atTop = axis.next_tier === null || axis.next_tier === undefined
          const nextLabel = atTop
            ? t("panel.axis.maxTier")
            : t("panel.axis.toNext", {
                tier: t(`panel.tier.${key}.${String(axis.next_tier)}`, { defaultValue: String(axis.next_tier) }),
                points: axis.to_next,
              })
          const delta = axis.delta_today
          return (
            <Card key={key} title={t(`panel.stat.${key}`)}>
              <Stack gap={8}>
                <Inline gap={8} align="center">
                  <Text>{String(Math.round(Number(axis.value ?? props.values[key] ?? 0)))}</Text>
                  <StatusBadge tone={tierTone(axis.tier_index)} label={tierName} />
                </Inline>
                <Progress value={Math.round(Number(axis.value ?? props.values[key] ?? 0))} />
                <Text>{sparkline(props.sparks[key] ?? [])}</Text>
                <Text>{nextLabel}</Text>
                {delta === null || delta === undefined ? null : (
                  <Text>{t("panel.axis.delta", { delta: `${delta > 0 ? "+" : ""}${delta}` })}</Text>
                )}
              </Stack>
            </Card>
          )
        })}
      </Columns>
    </Card>
  )
}
