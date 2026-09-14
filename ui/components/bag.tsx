// 她的背包（v0.6.0）：从"肉干: 3"的文字行升级成物品小卡——数量、效果、一键「给她」。
// 旧版一个下拉框+一个按钮的间接流（先选再按）改成每卡直达：在她自己的东西上，
// 不需要先"选中"再"操作"两步。照顾动作仍走 panel 传入的 onGive——门面调用
// 通道全仓只有一条（props 层不碰任何调用门面，见 panel.tsx 顶部注释）。

import { Button, Card, Columns, EmptyState, Stack, StatusBadge, Text } from "@neko/plugin-ui"
import type { Translate } from "../shared"

export type BagItem = {
  id: string
  label: string
  count: number
  effects: string
}

export function Bag(props: {
  t: Translate
  items: BagItem[]
  enabled: boolean
  onGive: (itemId: string) => Promise<void>
}) {
  const t = props.t
  return (
    <Card title={t("panel.section.bag")}>
      {props.items.length > 0 ? (
        <Columns cols={2} minWidth={150} gap={12}>
          {props.items.map((entry) => (
            <Card key={entry.id} title={entry.label}>
              <Stack gap={8}>
                <StatusBadge tone="info" label={`×${entry.count}`} />
                <Text>{entry.effects}</Text>
                <Button tone="primary" disabled={!props.enabled} onClick={() => props.onGive(entry.id)}>
                  {t("panel.bag.give")}
                </Button>
              </Stack>
            </Card>
          ))}
        </Columns>
      ) : (
        <EmptyState title={t("panel.section.bag")} description={t("panel.bag.empty")} />
      )}
    </Card>
  )
}
