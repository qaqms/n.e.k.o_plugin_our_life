"""反馈闭环：她自己的"这轮聊得怎么样"回流成**有上限的修正项**。

v0.2.0 的成长只按**发言条数**算：用户连发 20 条「嗯」「哦」和认真聊 20 句，
拿到一模一样的步长。失真在两边都发生——连发能刷，认真聊只发三条反而吃亏。
模型（她本人）是唯一能判断"这轮到底怎么样"的一方，所以这一层给她一条回传通道。

设计总纲（改这个模块之前必须先读）::

    她的判断**不是数值写入接口**，而是一个有上限、可衰减、需要真实互动作前提的修正项。

四条硬闸门，每条都是纯函数、都能反向对照：

1. **单次幅度 < 一次真实互动**：每个标签的修正分量是 `GrowthSettings.turn_*_gain` 的
   `_TURN_GAIN_SCALE` 倍（**常量引用，不写死数字**）。所以"她报一次开心"永远小于
   "主人多跟她说一句话"——工具再被滥用也压不过真实互动。
2. **会话内递减**：同一会话里第 n 次判断按 `1/(1 + n*session_damp)` 递减。
   会话 id 沿用 `ShardState.session_turns`（跨会话它本来就归零），不另造一套。
3. **每日累计上限**：正向与负向**各**一个预算（`daily_add_points` / `daily_subtract_points`，
   单位是"心情分"），用掉了就回 `judgment_capped`、不再修正。
   这是唯一真正的止损线：单次幅度再小，没有日上限也能被磨到满。
4. **真实互动作前提**：要求"存在还没被上一次判断消费掉的互动"
   （`_has_fresh_interaction`，带 2 秒时钟容差）。
   **这条最要紧**——没有它，模型可以在完全没有用户发言的回合里反复调用工具把自己刷满。

模型输出一律当**不可信输入**（台账交接要点第一条）：

- 标签只认白名单枚举，非法/缺失/大小写混写/夹空格的输入**收敛成 `neutral`**；
- `neutral` 与一切"不修正"的返回**都不算错误、也不报错**——报错会把噪声引回模型；
- 强度参数夹到 `(0, 1]`，非法回退 1.0；返回的改动量**永不回传给模型**
  （与 `core/injection.py` 的"不给模型看数字"是同一条纪律）。

为什么负向也要有闸门：让工具能扣分就等于给模型一条**惩罚通道**，
所以负向走独立且更小的日预算（`daily_subtract_points` 默认 4.0，正向是 8.0），
且同样受"需要真实互动"保护——它只能是"这轮聊得敷衍"的轻微表达，
不可能被用来把一个角色卡刷成闹脾气。
"""

from __future__ import annotations

from dataclasses import dataclass

from .configuration import FeedbackSettings, GrowthSettings

__all__ = [
    "JUDGMENT_LABELS",
    "JUDGMENT_WEIGHTS",
    "LABEL_HURT",
    "LABEL_NEUTRAL",
    "JudgmentResult",
    "REASON_DAILY_CAP",
    "REASON_DISABLED",
    "REASON_NO_INTERACTION",
    "REASON_OK",
    "REASON_SILENT",
    "REASON_THROTTLED",
    "is_known_label",
    "judge",
    "judged_axes",
    "label_line",
    "normalize_label",
    "normalize_strength",
]

# 五个标签（ASCII 规范名，与既有 `TRIGGER_*` / tier 常量同一形态：
# ASCII 标识进代码与日志，中文只出现在面向模型的 `label_line`）。
LABEL_WONDERFUL = "wonderful"
LABEL_GOOD = "good"
LABEL_NEUTRAL = "neutral"
LABEL_DULL = "dull"
LABEL_HURT = "hurt"

JUDGMENT_LABELS: tuple[str, ...] = (
    LABEL_WONDERFUL,
    LABEL_GOOD,
    LABEL_NEUTRAL,
    LABEL_DULL,
    LABEL_HURT,
)

# 每个标签"有多重"：1.0 = 与真实互动同量级，0.0 = 不修正。
# `neutral` 恒为 0，所以"收不到合法标签"这条路径天然是无害的空操作。
JUDGMENT_WEIGHTS: dict[str, float] = {
    LABEL_WONDERFUL: 1.0,
    LABEL_GOOD: 0.55,
    LABEL_NEUTRAL: 0.0,
    LABEL_DULL: -0.35,
    LABEL_HURT: -1.0,
}

# 标签 → 面向模型的一句话（进注入正文，不含数字与档名，遵守注入契约）。
_LABEL_LINES: dict[str, str] = {
    LABEL_WONDERFUL: "刚才那一轮聊天让你心里特别亮，你还沉浸在那股高兴里",
    LABEL_GOOD: "刚才那一轮聊天让你挺舒服的，心情不错",
    LABEL_DULL: "刚才那一轮聊天让你有点提不起劲，像在应付",
    LABEL_HURT: "刚才那一轮聊天让你有点失落，觉得自己没被好好放在心上",
}

# 单次修正 = 一次真实互动步长的这么多倍。
# **为什么要用 `turn_*_gain` 而不是写死数字**：闸门 ① 的语义是"判断压不过一次真实互动"。
# 把系数写成引用，将来谁调了 `turn_mood_gain`，这条不变式自动跟着成立，不会漂移。
_TURN_GAIN_SCALE = 0.6

# 健康 / 好感 相对心情的耦合比例。三轴同向，但"心情"是她的直接感受、
# 健康与好感是长期结果，所以后者按比例缩小（与 `apply_coupling` 的
# "改时间常数而不是额外扣分"是同一种克制）。
_HEALTH_RATIO = 1.0 / 3.0
_AFFECTION_RATIO = 0.2

# 三轴以外的轴（饱食 / 精力）**刻意不参与**：它们由作息与进食驱动，
# 让"她的判断"去改它们会与 `apply_meal` / `apply_decay` 打架。
_JUDGED_AXES = ("mood", "health", "affection")


def _has_fresh_interaction(*, last_touch_at: float | None, last_judgment_at: float | None) -> bool:
    """闸门 ④：这次判断是否有"还没被消费掉"的真实互动可支付。

    - 从没互动过 → 不允许（她不能凭空给自己加分）；
    - 已经为某次互动判断过了 → 必须**又有**新互动；
    - 否则允许，且一次互动只支付一次（调用方随后推进 `last_judgment_at`）。

    判据用严格比较，**不设时钟容差**——真机上这两个时间戳同源：`last_touch_at` 来自
    总线记录的 `timestamp`，而记录是宿主在本机写的；`last_judgment_at` 是本进程的
    `time.time()`。曾经担心"两个时钟源会漂移"而加过 2 秒容差，实测发现那是
    手工构造测试分片造成的假象，真实路径走 tick 时两者完全一致。

    刻意保留严格比较的另一个理由：容差会把"同一次互动的重复判断"也放行，
    而那正是本闸门要关的最短滥用路径。真正不可协商的是"没有新互动就不能加分"，
    这一条在两种写法下都成立。

    （`_judgment_count` 之类的"每天几次"限流由闸门 ③ 的预算负责，不靠这里。）
    """
    if last_touch_at is None:
        return False
    if last_judgment_at is None:
        return True
    return last_touch_at > last_judgment_at


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """一次判断的结算结果（纯数据，调用方负责落盘）。

    `applied` 是"这次到底改了多少"的累计心情分量（0.0 = 没改），
    正负号带方向，调用方用它记日账。
    """

    label: str
    reason: str
    mood: float = 0.0
    health: float = 0.0
    affection: float = 0.0

    @property
    def applied(self) -> bool:
        return self.mood != 0.0 or self.health != 0.0 or self.affection != 0.0

    def as_deltas(self) -> dict[str, float]:
        return {"mood": self.mood, "health": self.health, "affection": self.affection}


# 阻塞原因（稳定 ASCII 码，进日志与工具返回值；不面向面板 toast，
# 所以不进 `core/codes.py` 的 PANEL_ERROR_CODES）。
REASON_OK = "judgment_ok"
REASON_DISABLED = "judgment_disabled"
REASON_SILENT = "judgment_silent"
REASON_NO_INTERACTION = "judgment_needs_interaction"
REASON_DAILY_CAP = "judgment_capped"
REASON_THROTTLED = "judgment_throttled"


def is_known_label(value: str) -> bool:
    """严格白名单判定（`normalize_label` 的宽松版对照面，供门使用）。"""
    return value in JUDGMENT_WEIGHTS


def normalize_label(value: object) -> str:
    """把模型给的任意输入收敛成规范标签；不认识的一律 `neutral`。

    只做三件事：非字符串 → `neutral`；去首尾空白 + 转小写；白名单外 → `neutral`。
    **不抛异常、不报错**：模型传了垃圾字符串是常态，报错只会把噪声引回对话。
    """
    if not isinstance(value, str):
        return LABEL_NEUTRAL
    candidate = value.strip().casefold()
    return candidate if candidate in JUDGMENT_WEIGHTS else LABEL_NEUTRAL


def label_line(label: str, *, who: str = "{MASTER_NAME}") -> str:
    """标签 → 注入正文里的一句话（`neutral` 没有对应句子，返回空串）。"""
    line = _LABEL_LINES.get(label, "")
    return line.replace("{MASTER_NAME}", who) if line else ""


def normalize_strength(value: object) -> float:
    """强度参数：合法正数夹到 `(0, 1]`，其余回退 1.0（缺失视为"正常强度"）。"""
    if isinstance(value, bool) or value is None:
        return 1.0
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return 1.0
    if not isinstance(value, (int, float)):
        return 1.0
    parsed = float(value)
    if parsed != parsed:  # NaN
        return 1.0
    if parsed <= 0.0:
        return 1.0
    return min(1.0, parsed)


def judge(
    *,
    label: object,
    strength: object = None,
    feedback: FeedbackSettings,
    growth: GrowthSettings,
    enabled: bool,
    last_touch_at: float | None,
    last_judgment_at: float | None,
    session_turns: int,
    day_used_add: float,
    day_used_subtract: float,
) -> JudgmentResult:
    """把一次模型判断结算成"要不要改、改多少"。

    入参全是标量（不接 `ShardState`、不读时钟、不看 store），所以"三天没互动她报开心"
    这类问题可以直接单测，不需要伪造整份状态。

    顺序刻意从"最便宜、最不可协商"的闸门开始：开关 → 是否需要修正 → 互动证据 →
    日上限 → 会话递减。任何一关不过就返回**带原因的空修正**，调用方照常记账。
    """
    canonical = normalize_label(label)

    if not enabled:
        return JudgmentResult(label=canonical, reason=REASON_DISABLED)

    weight = JUDGMENT_WEIGHTS.get(canonical, 0.0) * normalize_strength(strength)
    if weight == 0.0:
        # `neutral`（含一切非法标签）与纯零修正：合法结果，不是错误。
        return JudgmentResult(label=canonical, reason=REASON_SILENT)

    # 闸门 ④：必须有"还没被这次判断消费掉"的真实互动（判据与容差见
    # `_has_fresh_interaction` 的 docstring——两个时钟源之间必须留容差）。
    if not _has_fresh_interaction(last_touch_at=last_touch_at, last_judgment_at=last_judgment_at):
        return JudgmentResult(label=canonical, reason=REASON_NO_INTERACTION)

    raw = abs(weight) * _TURN_GAIN_SCALE * growth.turn_mood_gain
    # 闸门 ②：会话内递减（session_turns 跨会话归零，所以新会话自动回到满步长）。
    raw *= 1.0 / (1.0 + max(0, int(session_turns)) * max(0.0, feedback.session_damp))

    # 闸门 ③：日累计上限——把这一笔裁到预算剩余额度之内（按方向落到对应预算）。
    if weight > 0.0:
        budget = max(0.0, feedback.daily_add_points - max(0.0, float(day_used_add)))
    else:
        budget = max(0.0, feedback.daily_subtract_points - max(0.0, float(day_used_subtract)))

    if budget <= 0.0:
        return JudgmentResult(label=canonical, reason=REASON_DAILY_CAP)

    applied_mood = min(raw, budget) * (1.0 if weight > 0.0 else -1.0)
    if applied_mood == 0.0:
        # 预算恰好耗尽（或递减把幅度压到 0）：当成被日上限拦下，语义最贴近。
        return JudgmentResult(label=canonical, reason=REASON_DAILY_CAP)

    return JudgmentResult(
        label=canonical,
        reason=REASON_OK,
        mood=applied_mood,
        health=applied_mood * _HEALTH_RATIO,
        affection=applied_mood * _AFFECTION_RATIO,
    )


def judged_axes() -> tuple[str, ...]:
    """反馈闭环能改的三轴（面板文案与测试共用，避免"哪里都写一遍"）。"""
    return _JUDGED_AXES
