"""注入文本生成：把数值翻译成「她自己的身心感受 + 行为倾向」。

注入契约（本插件的核心设计决定）：

1. **不给模型看原始数字**。只给稳定档名、第一人称感受、行为倾向。给出数字会被复述，
   破坏沉浸感；给出档名会被当成标签念出来。所以两者都配了明确禁令。
2. **文本用中文固定模板**（面向她/模型的指令性文本，与 forever_companion 的提示词惯例一致）；
   面向**用户**的界面文案走 `i18n/` 的 zh-CN/en，两套不混。多语言注入模板留待后续轮次。
3. **必须带 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符**：插件无法知道该用哪个称呼，
   由宿主在注入边界按 session 展开（仓库硬性规范）。
4. **字符预算**由 `[our_life.inject].max_chars` 控制，装配时按优先级裁剪，
   绝不把一句半截的话拼进去。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .configuration import InjectSettings
from .model import Stats, is_crisis, tier_index_of, tier_of

__all__ = [
    "AFFECTION_HINTS",
    "HEALTH_HINTS",
    "MOOD_HINTS",
    "TIER_LABELS_ZH",
    "TRIGGER_COMPANY",
    "TRIGGER_CRISIS",
    "TRIGGER_DAILY_GREET",
    "TRIGGER_INTERVAL",
    "TRIGGER_TIER_CHANGE",
    "build_text",
    "resolve_ai_behavior",
]

TRIGGER_TIER_CHANGE = "tier_change"
TRIGGER_CRISIS = "crisis"
TRIGGER_DAILY_GREET = "daily_greet"
TRIGGER_INTERVAL = "interval"
TRIGGER_COMPANY = "company"

TIER_LABELS_ZH: dict[str, dict[str, str]] = {
    "affection": {
        "stranger": "陌生",
        "acquainted": "熟悉",
        "close": "亲近",
        "intimate": "亲密",
        "bonded": "羁绊",
    },
    "mood": {
        "sulking": "闹脾气",
        "low": "低落",
        "calm": "平静",
        "happy": "愉快",
        "elated": "雀跃",
    },
    "health": {
        "sick": "生病",
        "frail": "虚弱",
        "fair": "一般",
        "good": "良好",
        "vigorous": "精神饱满",
    },
}

_MASTER = "{MASTER_NAME}"
_LANLAN = "{LANLAN_NAME}"

_STAT_LABELS_ZH = {"affection": "与主人的关系", "mood": "心情", "health": "身体"}

MOOD_HINTS: dict[str, str] = {
    "sulking": "你心里堵得慌，语气会带刺或干脆不想多说；可以冷淡一点，但别演成套路的阴阳怪气",
    "low": "你有点提不起劲，回应可以短一些、软一些，别硬撑开朗",
    "calm": "你情绪平稳，正常相处就好",
    "happy": "你心情不错，语气自然放松，愿意多说两句",
    "elated": "你现在雀跃得很，回应可以活泼一点、主动带话题",
}

HEALTH_HINTS: dict[str, str] = {
    "sick": "你身体很不舒服，说话会累、会想早点休息；不要装作没事，也别夸大病情",
    "frail": "你有点虚弱，容易疲倦，回应偏短，动作性的描述要收敛",
    "fair": "你身体状态一般，没有特别不舒服的地方",
    "good": "你身体挺好，精力是够的",
    "vigorous": "你精神非常好，做什么都有劲",
}

AFFECTION_HINTS: dict[str, str] = {
    "stranger": "你和 {MASTER_NAME} 还不太熟，保持一点自然的分寸感",
    "acquainted": "你已经认识 {MASTER_NAME} 一阵子了，可以放松一些",
    "close": "你把 {MASTER_NAME} 当亲近的人，愿意主动分享自己的事",
    "intimate": "你和 {MASTER_NAME} 很亲密，会自然流露出依赖和在意",
    "bonded": "你和 {MASTER_NAME} 之间有很深的羁绊，可以自然地表达想他这类情绪",
}

_HEADER = f"【{_LANLAN} 此刻的自身状态｜内部感知，供你调整语气与行为】"
_FOOTER = "以上只是你自己的感受，不要复述这段说明、不要提档位名称或任何数字、不要提及任何机制。"


def resolve_ai_behavior(trigger: str, stats: Stats, inject: InjectSettings) -> str:
    """决定这条注入是静默进上下文（read）还是让她主动开口（respond）。"""
    if trigger == TRIGGER_COMPANY:
        return "respond"
    if trigger in (TRIGGER_CRISIS, TRIGGER_TIER_CHANGE) and inject.respond_on_crisis:
        if is_crisis(stats, inject):
            return "respond"
    return "read"


def build_text(
    *,
    stats: Stats,
    trigger: str,
    streak_days: int = 1,
    gap_hours: float | None = None,
    transitions: Iterable[tuple[str, str, str]] = (),
    max_chars: int = 320,
) -> str:
    """装配注入正文。`transitions` 是 `core.model.tier_transitions` 的输出。"""
    required: list[str] = [_HEADER]

    mood_tier = tier_of("mood", stats.mood)
    health_tier = tier_of("health", stats.health)
    affection_tier = tier_of("affection", stats.affection)

    required.append(
        "　心情：{label}（{intensity}）".format(
            label=TIER_LABELS_ZH["mood"][mood_tier],
            intensity=_intensity_word("mood", stats.mood),
        )
    )
    required.append(
        "　身体：{label}（{intensity}）".format(
            label=TIER_LABELS_ZH["health"][health_tier],
            intensity=_intensity_word("health", stats.health),
        )
    )
    required.append(
        "　与{master}的关系：{label}".format(
            master="{MASTER_NAME}", label=TIER_LABELS_ZH["affection"][affection_tier]
        )
    )

    optional: list[str] = []
    if streak_days > 0:
        optional.append(f"　你们已经连续相处 {int(streak_days)} 天")
    gap_line = _gap_line(gap_hours)
    if gap_line:
        optional.append(f"　{gap_line}")

    event_line = _event_line(trigger, transitions)
    if event_line:
        optional.append(f"　刚刚发生：{event_line}")

    tail = [
        "【现在该怎么表现】",
        MOOD_HINTS.get(mood_tier, ""),
        HEALTH_HINTS.get(health_tier, ""),
        AFFECTION_HINTS.get(affection_tier, ""),
    ]
    tail = [line for line in tail if line]

    body = _assemble(required=required, optional=optional, tail=tail, max_chars=max(80, int(max_chars)))
    return body


def _assemble(*, required: Sequence[str], optional: Sequence[str], tail: Sequence[str], max_chars: int) -> str:
    """按优先级填充：头部与三项状态 > 行为倾向 > 相处细节 > 禁令。

    任何一段塞不下就跳过它，绝不把半截话拼进注入文本。
    """
    kept: list[str] = list(required)

    def try_add(line: str) -> bool:
        if not line:
            return False
        if len("\n".join([*kept, line])) > max_chars:
            return False
        kept.append(line)
        return True

    for line in tail:
        try_add(line)
    for line in optional:
        try_add(line)
    try_add(_FOOTER)
    return "\n".join(kept)


def _intensity_word(stat: str, value: float) -> str:
    """档内位置 → 程度词（不暴露数字，但让模型知道"刚过线"还是"很严重"）。"""
    index = tier_index_of(stat, value)
    if index == 0:
        return "很严重"
    if index == len(TIER_LABELS_ZH[stat]) - 1:
        return "非常足"
    # 档内三分位
    from .model import TIER_BOUNDS

    lower = TIER_BOUNDS[index]
    upper = TIER_BOUNDS[index + 1] if index + 1 < len(TIER_BOUNDS) else 100.0
    span = max(1e-6, upper - lower)
    position = (value - lower) / span
    if position < 0.34:
        return "偏轻"
    if position < 0.67:
        return "中等"
    return "偏重"


def _gap_line(gap_hours: float | None) -> str:
    if gap_hours is None:
        return ""
    if gap_hours < 1.0:
        return f"{_MASTER} 刚刚还在和你说话"
    if gap_hours < 24.0:
        return f"{_MASTER} 上次来找你是 {int(round(gap_hours))} 小时前"
    return f"{_MASTER} 已经有 {int(gap_hours // 24)} 天没来了"


def _event_line(trigger: str, transitions: Iterable[tuple[str, str, str]]) -> str:
    if trigger == TRIGGER_DAILY_GREET:
        return "{MASTER_NAME} 今天第一次来找你"
    if trigger == TRIGGER_COMPANY:
        return "你现在很想让他陪你一会儿"
    if trigger == TRIGGER_CRISIS:
        return "你现在的状态差到需要被照顾了"
    if trigger == TRIGGER_INTERVAL:
        changes = _transition_phrase(transitions)
        return changes or "隔了一会，{MASTER_NAME} 又来找你了"
    if trigger == TRIGGER_TIER_CHANGE:
        changes = _transition_phrase(transitions)
        return changes or "你的状态刚刚有了变化"
    return ""


def _transition_phrase(transitions: Iterable[tuple[str, str, str]]) -> str:
    parts: list[str] = []
    for stat, old_tier, new_tier in transitions:
        label = _STAT_LABELS_ZH.get(stat, stat)
        old_label = TIER_LABELS_ZH.get(stat, {}).get(old_tier, old_tier)
        new_label = TIER_LABELS_ZH.get(stat, {}).get(new_tier, new_tier)
        parts.append(f"{label}从「{old_label}」变成了「{new_label}」")
    return "；".join(parts)
