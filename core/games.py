"""玩家打工（v0.7.0）：服务器权威小游戏——出题、判分、发钱全在后端。

为什么是"服务器权威"（设计决定，写死在这里）：
面板的 `api.call` 参数是用户可任意伪造的，**客户端上报的分数永远不可信**。
所以两个游戏的真值（题答、牌序）都存在分片里的 challenge 对象中，前端只拿到
"该看的部分"（题目文本、当前牌面），提交时后端对照自己存的真相判分。
作弊面收敛为"读自己的 store"——那是宿主用户数据目录，能改它的人本来就能直接
改金币，防它不属本插件的威胁模型。

两个游戏：

- **心算冲刺 `arith`**：后端生成 N 道两位整数加减与小数乘法题，只发题面不发答案；
  客户端在时限内提交答案数组，后端逐题判分。每题 `coin_per_correct`，全对加
  `perfect_bonus`。
- **猜大小 `hielo`**：后端一次抽好整副牌序存进 challenge，逐轮只公布当前牌；
  玩家押"下一张更大/更小"，后端翻自己存的牌。赢 `win_coins`、输 `loss_coins`
  （正期望但很小——它是打工不是赌场，输了也给安慰钱，赚多少仍然取决于玩几轮）。

判分纪律：

1. **一次性**：challenge 结算后置 `consumed`，重放同一份答案拿不到第二笔钱。
2. **时限**：`deadline_at` 之后提交一律 `game_expired`（宽限 5 秒只吸收网络抖动）。
   重新开局 = 旧题作废，**不退款不计数**——次数闸在"完成并发钱"那一步才动。
3. **次数账本在后端**：`game_day` + `game_counts`（按 kind + total）自己保证算的是
   今天的账（与签到/判断预算同一手法，入口先于 tick 被调用也正确）。
4. **坏输入静默收敛**：答案不是整数、bet 不是合法值 → 稳定错误码，不抛异常。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "GAME_KINDS",
    "SUBMIT_GRACE_SEC",
    "GameOutcome",
    "answer_invalid_reason",
    "build_challenge",
    "challenge_public_view",
    "consume_challenge",
    "daily_block_reason",
    "expired",
    "normalize_challenge",
    "normalize_game_counts",
    "submit_arith",
    "submit_hielo",
]

GAME_KINDS: tuple[str, ...] = ("arith", "hielo")
# 时限之外的提交宽限（秒）：吸收网络/面板卡顿，不吸收"睡一觉再来交卷"。
SUBMIT_GRACE_SEC = 5.0
# 猜大小的牌面是 1..13 的点数（花色纯装饰，判大小只看点数；同点按输）。
HIELO_RANKS = 13


# ---------------------------------------------------------------------------
# 次数账本
# ---------------------------------------------------------------------------


def normalize_game_counts(raw: Any) -> dict[str, int]:
    """`{kind|total: count}`，只收非负整数，其它键丢弃。"""
    out: dict[str, int] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if int(value) > 0:
            out[key] = int(value)
    return out


def daily_block_reason(
    *,
    kind: str,
    counts: Mapping[str, int],
    per_game_limit: int,
    total_limit: int,
) -> str:
    """开局前的次数闸：返回 `"ok"` 或 `game_daily_limit`/`game_invalid_kind`。"""
    if kind not in GAME_KINDS:
        return "game_invalid_kind"
    if counts.get(kind, 0) >= max(0, int(per_game_limit)):
        return "game_daily_limit"
    if counts.get("total", 0) >= max(0, int(total_limit)):
        return "game_daily_limit"
    return "ok"


# ---------------------------------------------------------------------------
# 挑战的生成与归一化
# ---------------------------------------------------------------------------


def _arith_problem(rng: random.Random) -> tuple[str, int]:
    """一道题：`(题面, 答案)`。难度钉在"两位整数加减 + 小乘法"——
    它是打工小游戏，不是奥数；时限才是成本。"""
    kind = rng.random()
    if kind < 0.4:
        left, right = rng.randint(10, 99), rng.randint(10, 99)
        return f"{left} + {right}", left + right
    if kind < 0.8:
        left, right = rng.randint(20, 99), rng.randint(1, 19)
        return f"{left} - {right}", left - right
    left, right = rng.randint(3, 9), rng.randint(11, 19)
    return f"{left} × {right}", left * right


def build_challenge(
    kind: str,
    *,
    now: float,
    rng: random.Random,
    arith_rounds: int,
    arith_time_limit_sec: float,
    hielo_rounds: int,
) -> dict[str, Any] | None:
    """签发一份挑战（存进分片的形状）。未知 kind 返回 None。

    challenge 里存着**全部真值**（题答、牌序），公开视图由
    `challenge_public_view` 现切——真值从不出后端。
    """
    if kind == "arith":
        problems = [_arith_problem(rng) for _ in range(max(1, int(arith_rounds)))]
        return {
            "kind": "arith",
            "issued_at": float(now),
            "deadline_at": float(now) + max(10.0, float(arith_time_limit_sec)),
            "questions": [[text, answer] for text, answer in problems],
            "consumed": False,
        }
    if kind == "hielo":
        ranks = [rng.randint(1, HIELO_RANKS) for _ in range(max(2, int(hielo_rounds) + 1))]
        return {
            "kind": "hielo",
            "issued_at": float(now),
            "deadline_at": None,  # 猜大小按轮推进，不设总时限（时限由每日次数闸约束）
            "ranks": ranks,
            "index": 0,  # 已揭示到第几张
            "rounds": max(1, int(hielo_rounds)),
            "wins": 0,
            "losses": 0,
            "consumed": False,
        }
    return None


def normalize_challenge(raw: Any) -> dict[str, Any] | None:
    """从 store 恢复挑战：认不出的形状一律 None（坏挑战宁可作废不可信）。"""
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind not in GAME_KINDS:
        return None
    issued = raw.get("issued_at")
    if isinstance(issued, bool) or not isinstance(issued, (int, float)):
        return None
    consumed = bool(raw.get("consumed"))
    if kind == "arith":
        questions_raw = raw.get("questions")
        if not isinstance(questions_raw, (list, tuple)) or not questions_raw:
            return None
        questions: list[tuple[str, int]] = []
        for row in questions_raw:
            if (
                isinstance(row, (list, tuple))
                and len(row) == 2
                and isinstance(row[0], str)
                and isinstance(row[1], (int, float))
                and not isinstance(row[1], bool)
            ):
                questions.append((row[0], int(row[1])))
            else:
                return None
        deadline = raw.get("deadline_at")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return None
        return {
            "kind": "arith",
            "issued_at": float(issued),
            "deadline_at": float(deadline),
            "questions": [list(row) for row in questions],
            "consumed": consumed,
        }
    ranks_raw = raw.get("ranks")
    if not isinstance(ranks_raw, (list, tuple)) or not ranks_raw:
        return None
    ranks: list[int] = []
    for rank in ranks_raw:
        if isinstance(rank, bool) or not isinstance(rank, (int, float)) or not 1 <= int(rank) <= HIELO_RANKS:
            return None
        ranks.append(int(rank))
    index = raw.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, (int, float)) or not 0 <= int(index) < len(ranks):
        return None
    return {
        "kind": "hielo",
        "issued_at": float(issued),
        "deadline_at": None,
        "ranks": ranks,
        "index": int(index),
        "rounds": max(1, _as_pos_int(raw.get("rounds"), len(ranks) - 1 or 1)),
        "wins": _as_pos_int(raw.get("wins"), 0),
        "losses": _as_pos_int(raw.get("losses"), 0),
        "consumed": consumed,
    }


def _as_pos_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return max(0, int(default))
    return max(0, int(value))


def challenge_public_view(challenge: Mapping[str, Any]) -> dict[str, Any]:
    """前端能看到的形状：arith 只给题面；hielo 只给当前牌与进度。**答案不在其中**。"""
    kind = challenge.get("kind")
    if kind == "arith":
        questions = challenge.get("questions") or []
        return {
            "kind": "arith",
            "issued_at": challenge.get("issued_at"),
            "deadline_at": challenge.get("deadline_at"),
            "questions": [row[0] for row in questions if isinstance(row, (list, tuple)) and row],
        }
    if kind == "hielo":
        ranks = challenge.get("ranks") or []
        index = int(challenge.get("index") or 0)
        current = int(ranks[index]) if 0 <= index < len(ranks) else None
        revealed = [int(rank) for rank in ranks[: index + 1]]
        return {
            "kind": "hielo",
            "current": current,
            "revealed": revealed,
            "round": min(index + 1, int(challenge.get("rounds") or 1)),
            "rounds": int(challenge.get("rounds") or 1),
            "wins": int(challenge.get("wins") or 0),
            "losses": int(challenge.get("losses") or 0),
        }
    return {"kind": str(kind or "")}


def consume_challenge(challenge: dict[str, Any]) -> dict[str, Any]:
    """结算后置一次性标记。重放的提交会先撞上 `game_no_challenge`。"""
    challenge["consumed"] = True
    return challenge


# ---------------------------------------------------------------------------
# 判分
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GameOutcome:
    """一局的结果（发多少钱的唯一出口）。"""

    kind: str
    correct: int
    rounds: int
    coins: int
    perfect: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "correct": self.correct,
            "rounds": self.rounds,
            "coins": self.coins,
            "perfect": self.perfect,
        }


def expired(challenge: Mapping[str, Any], *, now: float) -> bool:
    deadline = challenge.get("deadline_at")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return False
    return float(now) > float(deadline) + SUBMIT_GRACE_SEC


def answer_invalid_reason(answers: Any) -> str:
    """交卷入参的形状闸：非列表/项非整数 → 稳定码（不抛）。"""
    if not isinstance(answers, (list, tuple)) or not answers:
        return "game_answer_invalid"
    for item in answers:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return "game_answer_invalid"
    return ""


def submit_arith(
    challenge: Mapping[str, Any],
    answers: Iterable[Any],
    *,
    now: float,
    coin_per_correct: int,
    perfect_bonus: int,
) -> GameOutcome | None:
    """心算判分：逐题对答案。返回 None = 挑战不可信/类型不对（调用方回错误码）。"""
    if challenge.get("kind") != "arith" or expired(challenge, now=now):
        return None
    questions = challenge.get("questions")
    if not isinstance(questions, (list, tuple)):
        return None
    answer_list = list(answers)
    correct = 0
    for position, row in enumerate(questions):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        given = answer_list[position] if position < len(answer_list) else None
        if isinstance(given, bool) or not isinstance(given, (int, float)):
            continue
        if int(given) == int(row[1]):
            correct += 1
    rounds = len(questions)
    coins = max(0, int(coin_per_correct)) * correct
    perfect = rounds > 0 and correct == rounds
    if perfect:
        coins += max(0, int(perfect_bonus))
    return GameOutcome(kind="arith", correct=correct, rounds=rounds, coins=max(0, int(coins)), perfect=perfect)


def submit_hielo(
    challenge: dict[str, Any],
    bet: str,
    *,
    win_coins: int,
    loss_coins: int,
) -> tuple[GameOutcome | None, dict[str, Any], str]:
    """猜大小推进一轮。返回 `(终局结果或 None, 更新后的挑战, 错误码)`。

    牌序在签发时就抽好了——这里只是"翻下一张自己存的牌"，没有任何时刻需要
    再次随机，所以前端拿到当前牌后无论怎么拖延、重发，下一张都不会变。
    最后一轮结算完，`GameOutcome` 非 None 且 challenge 被 consume。
    """
    if challenge.get("kind") != "hielo":
        return None, challenge, "game_no_challenge"
    if bet not in ("higher", "lower"):
        return None, challenge, "game_answer_invalid"
    ranks = challenge.get("ranks") or []
    index = int(challenge.get("index") or 0)
    rounds = int(challenge.get("rounds") or 0)
    if index + 1 >= len(ranks) or index >= rounds:
        return None, challenge, "game_no_challenge"
    current = int(ranks[index])
    nxt = int(ranks[index + 1])
    # 同点按输：判据必须和 UI 公示的规则一致（panel.hielo.tieNote）。
    won = nxt > current if bet == "higher" else nxt < current
    wins = int(challenge.get("wins") or 0) + (1 if won else 0)
    losses = int(challenge.get("losses") or 0) + (0 if won else 1)
    challenge["index"] = index + 1
    challenge["wins"] = wins
    challenge["losses"] = losses
    finished = challenge["index"] >= rounds
    outcome: GameOutcome | None = None
    if finished:
        coins = wins * max(0, int(win_coins)) + losses * max(0, int(loss_coins))
        outcome = GameOutcome(
            kind="hielo",
            correct=wins,
            rounds=rounds,
            coins=max(0, int(coins)),
            perfect=rounds > 0 and wins == rounds,
        )
        consume_challenge(challenge)
    return outcome, challenge, ""
