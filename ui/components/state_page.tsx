// 「她此刻的状态」页（v0.8.0）：她的自述 + 四段状态档案。
//
// 分工纪律（与日历/打工板一致）：**判据全在后端**——挑哪几句自述、危机时多说几句、
// 哪两根轴正在互相拖累，都是 `core/state_note.py` 算好的；这个组件只做两件事：
// 把键名翻成文案、把数摆出来。它自己**不比较档位、不折算耦合、不决定句数**。
//
// 为什么不复用总览的五轴卡（`axis_cards.tsx`）：那张卡回答"每一项现在多少"，
// 这一页回答"她现在怎么样、为什么"。所以身体段用的是**带因果的行**（距下一档 +
// 今日变化 + 跨轴拖累），而不是五条进度条的另一种摆法。
//
// 文案：`voice` 是**整句 i18n 键**，组件按后端给的顺序逐句 `t()`——绝不逐词拼接，
// 中英词序不同，拼词在 en 下必坏。所有动态键（`panel.stateVoice.<轴>.<档>` /
// `panel.stateCoupling.<码>` / `panel.judgment.<标签>`）都由 `test_i18n_contract.py`
// 的结构门钉着，缺文案会在CI红，而不是在面板上显示成空白。

import {
  Card,
  Columns,
  Divider,
  Inline,
  KeyValue,
  Progress,
  Stack,
  StatCard,
  StatusBadge,
  Text,
} from "@neko/plugin-ui"
import { camel, formatGap, sparkline, tierTone } from "../shared"
import type { AnniversaryView, StateNote, Translate } from "../shared"

/** 自述段：她说出口的那 1~3 句。空数组时给一句诚实的"她没说"，而不是留白。 */
function VoiceCard(props: { t: Translate; note: StateNote }) {
  const t = props.t
  const voice = props.note.voice ?? []
  return (
    <Card title={t("panel.state.title")}>
      <Stack gap={8}>
        {voice.length === 0 ? (
          <Text>{t("panel.state.noVoice")}</Text>
        ) : (
          voice.map((key) => <Text key={key}>“{t(key)}”</Text>)
        )}
        <Text>{t("panel.state.subtitle")}</Text>
      </Stack>
    </Card>
  )
}

/** 身体段：逐轴一行（数值 + 档名 + 走势 + 距下一档 / 今日变化）。
 *
 * `to_next`/`delta_today` 可能是 `null`——前者是"已经封顶"，后者是"今天还没有锚点"。
 * 这两种情况**都不许显示成 0**：拿 0 冒充"今天没变"是编数据，所以行内直接不渲染。
 */
function BodyCard(props: { t: Translate; note: StateNote; sparks: Record<string, number[]> }) {
  const t = props.t
  const rows = props.note.body ?? []
  const coupling = props.note.coupling ?? []
  return (
    <Card title={t("panel.state.body")}>
      <Stack gap={10}>
        {rows.map((row) => {
          const stat = String(row.stat ?? "")
          const value = Math.round(Number(row.value ?? 0))
          const atTop = row.next_tier === null || row.next_tier === undefined
          const delta = row.delta_today
          return (
            <Stack key={stat} gap={4}>
              <Inline gap={8} align="center" wrap>
                <Text>{t(`panel.stat.${stat}`)}</Text>
                <StatusBadge
                  tone={tierTone(row.tier_index)}
                  label={t(`panel.tier.${stat}.${row.tier ?? "unknown"}`, { defaultValue: row.tier ?? "-" })}
                />
                <Text>{String(value)}</Text>
                <Text>{sparkline(props.sparks[stat] ?? [])}</Text>
              </Inline>
              <Progress value={value} />
              <Inline gap={12} align="center" wrap>
                <Text>
                  {atTop
                    ? t("panel.axis.maxTier")
                    : t("panel.axis.toNext", {
                        tier: t(`panel.tier.${stat}.${String(row.next_tier)}`, {
                          defaultValue: String(row.next_tier),
                        }),
                        points: row.to_next,
                      })}
                </Text>
                {delta === null || delta === undefined ? null : (
                  <Text>{t("panel.axis.delta", { delta: `${delta > 0 ? "+" : ""}${delta}` })}</Text>
                )}
              </Inline>
            </Stack>
          )
        })}
        <Divider />
        <Text>{t("panel.state.coupling")}</Text>
        {coupling.length === 0 ? (
          <Text>{t("panel.state.noCoupling")}</Text>
        ) : (
          coupling.map((code) => (
            <StatusBadge
              key={code}
              tone={code.endsWith("severe") ? "danger" : "warning"}
              label={t(`panel.stateCoupling.${camel(code)}`, { defaultValue: code })}
            />
          ))
        )}
      </Stack>
    </Card>
  )
}

/** 作息段：时段 + 在不在睡 + 距边界还有多久。钟点是纯数字，不需要文案。 */
function RhythmSection(props: { t: Translate; note: StateNote }) {
  const t = props.t
  const rhythm = props.note.rhythm ?? {}
  const sleeping = rhythm.sleeping === true
  const clock =
    rhythm.hour === null || rhythm.hour === undefined
      ? null
      : `${String(rhythm.hour).padStart(2, "0")}:${String(rhythm.minute ?? 0).padStart(2, "0")}`
  const toBoundary = sleeping ? rhythm.hours_to_wake : rhythm.hours_to_sleep
  return (
    <Card title={t("panel.state.rhythm")}>
      <Stack gap={10}>
        <Inline gap={8} align="center" wrap>
          <StatusBadge
            tone={sleeping ? "info" : "default"}
            label={t(`panel.phase.${rhythm.phase ?? "noon"}`, { defaultValue: rhythm.phase ?? "-" })}
          />
          {clock ? <Text>{clock}</Text> : null}
          {sleeping ? <StatusBadge tone="info" label={t("panel.band.sleeping")} /> : null}
        </Inline>
        <KeyValue
          items={[
            {
              key: "boundary",
              label: t(sleeping ? "panel.field.boundaryWake" : "panel.field.boundarySleep"),
              value:
                toBoundary === null || toBoundary === undefined
                  ? "-"
                  : t(sleeping ? "panel.band.toWake" : "panel.band.toSleep", {
                      minutes: Math.ceil(Number(toBoundary) * 60),
                    }),
            },
          ]}
        />
      </Stack>
    </Card>
  )
}

/** 我们段：相处天数 / 连续陪伴 / 距上次互动 / 纪念日。
 *
 * `gap_hours` 为 `null` 表示台账里根本没有"上次互动"（新角色），`formatGap` 会给出
 * 它自己的"从未"文案——与"刚刚说过话（0 小时）"绝不混同。
 */
function UsSection(props: { t: Translate; note: StateNote; anniversary?: AnniversaryView | null }) {
  const t = props.t
  const us = props.note.us ?? {}
  const anniversary = props.anniversary ?? null
  return (
    <Card title={t("panel.state.us")}>
      <Stack gap={10}>
        <Inline gap={8} align="center" wrap>
          <StatusBadge tone="info" label={t("panel.band.day", { day: us.day_number ?? 0 })} />
          <StatusBadge tone="default" label={t("panel.band.streak", { streak: us.streak_days ?? 0 })} />
        </Inline>
        <KeyValue
          items={[
            { key: "touch", label: t("panel.field.lastTouch"), value: formatGap(us.gap_hours, t) },
            {
              key: "anniversary",
              label: t("panel.field.anniversary"),
              value: anniversary
                ? anniversary.years
                  ? t("panel.anniversary.yearly", { years: anniversary.years })
                  : t("panel.anniversary.milestone", { day: anniversary.day_number ?? 0 })
                : t("panel.never"),
            },
          ]}
        />
      </Stack>
    </Card>
  )
}

/** 她的想法段：她自己判断"刚才这轮聊得怎么样"，把这个私密通道摊给主人看（v0.8.0 确认项）。
 *
 * 只给**最近一次**标签与实际改动的心情分。`applied` 刻意如实带符号：这是这一页少数几个
 * "她说不清但数值说得清"的东西，藏起来就等于回到"成长只按发言条数算"的失真。
 */
function MindCard(props: { t: Translate; note: StateNote }) {
  const t = props.t
  const mind = props.note.mind ?? {}
  const applied = mind.applied
  return (
    <Card title={t("panel.state.mind")}>
      <Stack gap={10}>
        {mind.has_judgment ? (
          <Inline gap={8} align="center" wrap>
            <StatusBadge
              tone={(mind.applied ?? 0) < 0 ? "warning" : "success"}
              label={t(`panel.judgment.${mind.label ?? "neutral"}`, { defaultValue: mind.label ?? "-" })}
            />
            {applied === null || applied === undefined ? null : (
              <Text>{t("panel.state.mindApplied", { delta: `${applied > 0 ? "+" : ""}${applied}` })}</Text>
            )}
          </Inline>
        ) : (
          <Text>{t("panel.state.mindNone")}</Text>
        )}
        {mind.count_today === null || mind.count_today === undefined ? null : (
          <Text>{t("panel.state.mindCount", { count: mind.count_today })}</Text>
        )}
      </Stack>
    </Card>
  )
}

/** 她的一天段：吃了几餐 / 签没签 / 在不在班上 / 有没有开着的局 / 今天开口几次。 */
function TodayCard(props: { t: Translate; note: StateNote }) {
  const t = props.t
  const today = props.note.today ?? {}
  const working = Boolean(today.job_id)
  const minutesLeft = Math.ceil(Number(today.job_remaining_sec ?? 0) / 60)
  return (
    <Card title={t("panel.state.today")}>
      <Stack gap={10}>
        <Columns cols={4} minWidth={120} gap={12}>
          <StatCard label={t("panel.today.meals")} value={String(today.meals_today ?? 0)} />
          <StatCard label={t("panel.today.spoke")} value={String(today.spoke_today ?? 0)} />
          <StatCard label={t("panel.field.sodas")} value={String(today.sodas ?? 0)} />
          <StatCard
            label={t("panel.field.mealsToday")}
            value={today.checked_today ? t("panel.cal.checkedToday") : t("panel.state.notChecked")}
          />
        </Columns>
        <Inline gap={8} align="center" wrap>
          <StatusBadge
            tone={working ? "info" : "default"}
            label={working ? t("panel.job.active", { minutes: minutesLeft }) : t("panel.state.noJob")}
          />
          {today.game_active ? <StatusBadge tone="warning" label={t("panel.state.gamePending")} /> : null}
        </Inline>
      </Stack>
    </Card>
  )
}

export function StatePage(props: {
  t: Translate
  note: StateNote
  sparks: Record<string, number[]>
  anniversary?: AnniversaryView | null
}) {
  const t = props.t
  const note = props.note ?? {}
  // 危机不在这一页另开一套：危机徽章已经在顶部状态带里（每一页都能看到），
  // 再在本页标一遍就是把同一件事说两遍；本组件只负责把自述与档案摆好。
  return (
    <Stack gap={16}>
      <VoiceCard t={t} note={note} />
      <BodyCard t={t} note={note} sparks={props.sparks} />
      <RhythmSection t={t} note={note} />
      <UsSection t={t} note={note} anniversary={props.anniversary} />
      <MindCard t={t} note={note} />
      <TodayCard t={t} note={note} />
    </Stack>
  )
}
