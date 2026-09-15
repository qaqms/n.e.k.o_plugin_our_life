// 玩家打工小游戏（v0.7.0）：心算冲刺 + 猜大小。
//
// 状态哲学与整个面板一致：**后端是唯一真相**。
// - 题目/当前牌/进度全部读 `props.games.active`（context 里已经切掉答案的公开视图），
//   面板刷新、切走再回来、甚至重开宿主都续得上局；
// - 本地 `useLocalState` 只存"心算答案草稿"（用户正在输入的东西），
//   且按挑战的 `issued_at` 归档——换一局，旧草稿自动作废。
// - 判分、发钱、额度全在 Python 侧；这里连"对了几题"都不计算，只渲染返回结果。
//
// 倒计时是本地钟对着 `deadline_at` 画的——到期后按钮变灰只是体验，
// 真判据是后端 `submit_arith` 的时限检查（前端时间不可信，所以不拿它做任何裁决）。

import {
  Button,
  Card,
  Columns,
  Field,
  Inline,
  NumberInput,
  Stack,
  StatusBadge,
  Text,
  useEffect,
  useLocalState,
  useState,
} from "@neko/plugin-ui"
import type { GamesView, Translate } from "../shared"

/** 1..13 点数 → 人读牌面（无花色：花色纯装饰，判大小只看点数）。 */
function rankLabel(rank: number | null | undefined): string {
  if (rank === null || rank === undefined) return "-"
  if (rank === 1) return "A"
  if (rank === 11) return "J"
  if (rank === 12) return "Q"
  if (rank === 13) return "K"
  return String(rank)
}

export function Minigames(props: {
  t: Translate
  games: GamesView
  masterEnabled: boolean
  onStart: (kind: string) => Promise<void>
  onSubmitArith: (answers: number[]) => Promise<void>
  onBet: (bet: string) => Promise<void>
}) {
  const t = props.t
  const games = props.games ?? {}
  const active = games.active ?? null
  const counts = games.counts ?? {}
  const totalUsed = Number(counts.total ?? 0)
  const totalLimit = Number(games.total_daily_limit ?? 0)
  const perLimit = Number(games.per_game_daily_limit ?? 0)
  const [clock, setClock] = useState<number>(0)
  const [draft, setDraft] = useLocalState<Record<string, Record<string, number>>>(
    "our_life.games.arith_draft",
    {}
  )

  // 本地秒表：只为把倒计时画出来；1s 一跳。
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const featureOn = props.masterEnabled && games.feature_enabled === true
  const quotaLeft = totalUsed < totalLimit
  const canStart = (kind: string) =>
    featureOn && active === null && quotaLeft && Number(counts[kind] ?? 0) < perLimit

  const arithParams = games.arith ?? {}
  const hieloParams = games.hielo ?? {}
  const arithActive = active !== null && active.kind === "arith"
  const hieloActive = active !== null && active.kind === "hielo"
  const questions =
    active !== null && active.kind === "arith" && Array.isArray(active.questions) ? active.questions : []
  const draftKey = String(active?.issued_at ?? "none")
  const answers = draft[draftKey] ?? {}
  const deadlineMs = Number(active?.deadline_at ?? 0) * 1000
  const secondsLeft = deadlineMs > 0 ? Math.max(0, Math.ceil((deadlineMs - clock) / 1000)) : null

  const setAnswer = (index: number, value: number | "") => {
    const bucket: Record<string, Record<string, number>> = { ...draft }
    const current: Record<string, number> = { ...(bucket[draftKey] ?? {}) }
    if (value === "") delete current[String(index)]
    else current[String(index)] = Number(value)
    bucket[draftKey] = current
    setDraft(bucket)
  }

  const submitArith = async () => {
    const ordered: number[] = []
    for (let index = 0; index < questions.length; index += 1) {
      const value = answers[String(index)]
      ordered.push(typeof value === "number" ? value : -999999)
    }
    await props.onSubmitArith(ordered)
  }

  return (
    <Card title={t("panel.game.title")}>
      <Stack gap={12}>
        <Inline gap={8} align="center" wrap>
          <StatusBadge tone={quotaLeft ? "default" : "warning"} label={t("panel.game.quota", { used: totalUsed, total: totalLimit })} />
          {games.last && games.last.kind ? (
            <Text>
              {t("panel.game.last", {
                kind: t(`panel.game.${games.last.kind}`, { defaultValue: String(games.last.kind) }),
                correct: games.last.correct ?? 0,
                rounds: games.last.rounds ?? 0,
                coins: games.last.coins ?? 0,
              })}
            </Text>
          ) : null}
        </Inline>

        <Columns cols={2} minWidth={280} gap={12}>
          <Card title={t("panel.game.arith")}>
            <Stack gap={8}>
              <Text>
                {t("panel.game.arithDesc", {
                  rounds: arithParams.rounds ?? 0,
                  seconds: arithParams.time_limit_sec ?? 0,
                  coins: arithParams.coin_per_correct ?? 0,
                  bonus: arithParams.perfect_bonus ?? 0,
                })}
              </Text>
              {arithActive ? (
                <>
                  {secondsLeft !== null ? (
                    <StatusBadge
                      tone={secondsLeft <= 10 ? "danger" : "info"}
                      label={t("panel.game.remaining", { sec: secondsLeft })}
                    />
                  ) : null}
                  <Columns cols={2} minWidth={130} gap={8}>
                    {questions.map((question: string, index: number) => (
                      <Field key={`${draftKey}-${index}`} label={question}>
                        <NumberInput
                          value={answers[String(index)] ?? ""}
                          onChange={(next: number | string) =>
                            setAnswer(index, next === "" ? "" : Number(next))
                          }
                        />
                      </Field>
                    ))}
                  </Columns>
                  <Button tone="success" disabled={!featureOn} onClick={submitArith}>
                    {t("actions.gameSubmit.label")}
                  </Button>
                </>
              ) : (
                <Button tone="primary" disabled={!canStart("arith")} onClick={async () => props.onStart("arith")}>
                  {t("panel.game.start")}
                </Button>
              )}
            </Stack>
          </Card>

          <Card title={t("panel.game.hielo")}>
            <Stack gap={8}>
              <Text>
                {t("panel.game.hieloDesc", {
                  rounds: hieloParams.rounds ?? 0,
                  win: hieloParams.win_coins ?? 0,
                  loss: hieloParams.loss_coins ?? 0,
                })}
              </Text>
              <Text>{t("panel.game.tieNote")}</Text>
              {hieloActive ? (
                <>
                  <Inline gap={8} align="center" wrap>
                    <StatusBadge tone="info" label={rankLabel(active.current)} />
                    <Text>
                      {t("panel.game.progress", {
                        round: active.round ?? 0,
                        rounds: active.rounds ?? 0,
                        wins: active.wins ?? 0,
                        losses: active.losses ?? 0,
                      })}
                    </Text>
                  </Inline>
                  <Inline gap={8}>
                    <Button tone="primary" disabled={!featureOn} onClick={async () => props.onBet("higher")}>
                      {t("panel.game.higher")}
                    </Button>
                    <Button tone="primary" disabled={!featureOn} onClick={async () => props.onBet("lower")}>
                      {t("panel.game.lower")}
                    </Button>
                  </Inline>
                </>
              ) : (
                <Button tone="primary" disabled={!canStart("hielo")} onClick={async () => props.onStart("hielo")}>
                  {t("panel.game.start")}
                </Button>
              )}
            </Stack>
          </Card>
        </Columns>
      </Stack>
    </Card>
  )
}
