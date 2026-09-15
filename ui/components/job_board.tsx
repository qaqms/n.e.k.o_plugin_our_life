// 打工板（v0.7.0）：她在不在班上、今天还剩几次、三份工各自的"价目表"。
//
// 分工纪律与日历一致：**这里只摆后端算好的数**（工钱区间、损耗、门槛都来自
// `core/jobs.py` 单一来源），组件不折算系数、不比较状态——那些判断在
// `job_start` 入口里，按钮只管把选择送回去，被拒时错误码经 toast 如实呈现。
//
// 倒计时是纯展示：`remaining_sec` 来自 context 快照（10s 自动刷新会把它拉近），
// 结算的真判据是 tick 里的 `now >= job_end_at`，与面板开不开、看不看无关。

import {
  Button,
  Card,
  Columns,
  Inline,
  KeyValue,
  Stack,
  StatusBadge,
  Text,
} from "@neko/plugin-ui"
import type { JobCatalogEntry, JobView, Translate } from "../shared"

function costText(entry: [string, number], t: Translate, sign: string): string {
  return `${t(`panel.stat.${entry[0]}`, { defaultValue: entry[0] })} ${sign}${entry[1]}`
}

export function JobBoard(props: {
  t: Translate
  job: JobView
  masterEnabled: boolean
  onStart: (jobId: string) => Promise<void>
  onReturn: () => Promise<void>
}) {
  const t = props.t
  const job = props.job ?? {}
  const catalog = job.catalog ?? []
  const working = Boolean(job.id)
  const minutesLeft = Math.ceil(Number(job.remaining_sec ?? 0) / 60)
  const todayCount = Number(job.today_count ?? 0)
  const maxPerDay = Number(job.max_per_day ?? 0)
  const canStartMore = props.masterEnabled && job.feature_enabled === true && !working && todayCount < maxPerDay

  return (
    <Card title={t("panel.job.title")}>
      <Stack gap={12}>
        <Inline gap={8} align="center" wrap>
          {working ? (
            <StatusBadge tone="info" label={t("panel.job.active", { minutes: minutesLeft })} />
          ) : (
            <StatusBadge tone="default" label={t("panel.job.off")} />
          )}
          {working ? (
            <Button tone="warning" disabled={!props.masterEnabled} onClick={props.onReturn}>
              {t("panel.job.early")}
            </Button>
          ) : null}
        </Inline>
        <KeyValue
          items={[
            {
              key: "today",
              label: t("panel.job.today"),
              value: `${todayCount} / ${maxPerDay}`,
            },
            { key: "total", label: t("panel.job.total"), value: String(job.shifts_total ?? 0) },
            { key: "earned", label: t("panel.job.earned"), value: String(job.earned_total ?? 0) },
          ]}
        />
        <Columns cols={3} minWidth={190} gap={12}>
          {catalog.map((entry: JobCatalogEntry) => (
            <Card key={String(entry.id)} title={t(`panel.job.${entry.id ?? "unknown"}`, { defaultValue: entry.id ?? "-" })}>
              <Stack gap={8}>
                <Text>{t("panel.job.pay", { low: entry.pay_low ?? 0, high: entry.pay_high ?? 0, hours: entry.hours ?? 0 })}</Text>
                <Text>
                  {(entry.costs ?? []).map((pair) => costText(pair, t, "−")).join(" · ")}
                </Text>
                {(entry.requires ?? []).length > 0 ? (
                  <Text>
                    {t("panel.job.requires", {
                      line: (entry.requires ?? []).map((pair) => costText(pair, t, "≥")).join(" · "),
                    })}
                  </Text>
                ) : null}
                <Button
                  tone="primary"
                  disabled={!canStartMore}
                  onClick={async () => {
                    await props.onStart(String(entry.id))
                  }}
                >
                  {t("actions.jobStart.label")}
                </Button>
              </Stack>
            </Card>
          ))}
        </Columns>
        <Text>{t("panel.job.hint", { ratio: Math.round((job.early_leave_ratio ?? 0.6) * 100) })}</Text>
      </Stack>
    </Card>
  )
}
