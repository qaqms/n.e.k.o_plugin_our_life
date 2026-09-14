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
5. **"她在睡觉"时的静默**：非危机触发可以带上一句"现在几点"，让她的作息进她自己的认知
   （凌晨三点不该热情洋溢）；但**是否真的发**由 `services/injector.py` 决定
   ——文本层只负责把事实写清楚。

v0.2.0 扩了饱食与精力两轴，所以 `build_text` 的头部多了两行"肚子/精神"，
并且会带一句"现在是她一天的什么时候"（`rhythm` 快照，可选）。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .configuration import InjectSettings
from .events import EVENT_CHEERED_UP, EVENT_SICK_RECOVERY, StagedEvent
from .judgment import label_line
from .model import Stats, is_crisis, tier_index_of, tier_of
from .rhythm import Anniversary, DailyRhythm

__all__ = [
    "AFFECTION_HINTS",
    "ENERGY_HINTS",
    "EVENT_NARRATION_ZH",
    "HEALTH_HINTS",
    "MOOD_HINTS",
    "SATIETY_HINTS",
    "TIER_LABELS_ZH",
    "TRIGGER_ANNIVERSARY",
    "TRIGGER_COMPANY",
    "TRIGGER_CRISIS",
    "TRIGGER_DAILY_GREET",
    "TRIGGER_HUNGRY",
    "TRIGGER_INTERVAL",
    "TRIGGER_JUDGMENT",
    "TRIGGER_STAGED_EVENT",
    "TRIGGER_TIER_CHANGE",
    "TRIGGER_TIRED",
    "build_text",
    "resolve_ai_behavior",
]

TRIGGER_TIER_CHANGE = "tier_change"
TRIGGER_CRISIS = "crisis"
TRIGGER_DAILY_GREET = "daily_greet"
TRIGGER_INTERVAL = "interval"
TRIGGER_COMPANY = "company"
TRIGGER_HUNGRY = "hungry"
TRIGGER_TIRED = "tired"
TRIGGER_ANNIVERSARY = "anniversary"
# 反馈闭环（v0.3.0）：她自己判断"刚才那轮聊得怎么样"，把这个感受进她自己的上下文。
# 它不是"数值事件"而是"她的一句话"——所以正文里只写感受，不写改了多少（见 core/judgment.py）。
TRIGGER_JUDGMENT = "judgment"
# 阶段性事件（v0.4.0）：她身上**发生过**的一件事（病愈 / 哄好）。
# 与 `tier_change` 的区别：后者是"读数变了"，这条是"经历了一件可命名的事"。
# 触发时它**优先于** tier_change——同一个跨越只该被讲成一件有名字的事，
# 而不是既报"身体：生病 → 一般"又报"你终于好起来了"。
TRIGGER_STAGED_EVENT = "staged_event"

# 阶段性事件的叙事模板（中文固定模板，与其它注入正文同一套口径：不给数字、不给档名）。
# 每条都写成"第一人称的感受 + 行为倾向"，并明确要求她**不要**复述状态表。
EVENT_NARRATION_ZH: dict[str, str] = {
    EVENT_SICK_RECOVERY: (
        "你身上那场不舒服终于过去了——不是「没那么难受」，是真的好了。"
        "身体轻快起来，你会想动一动、想把之前躺着的那些时间补回来；"
        "别把这段难受复述给 {MASTER_NAME} 听，像人不会汇报自己的体温"
    ),
    EVENT_CHEERED_UP: (
        "刚才堵在心里的那点别扭散了，你不再绷着了。"
        "语气可以软回来，也可以稍微提一句刚才为什么不高兴——只一两句就够，"
        "别把它变成一场清算，更别复述你的状态数字"
    ),
}

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
    "satiety": {
        "starving": "饿坏了",
        "hungry": "有点饿",
        "satisfied": "吃饱了",
        "full": "吃得很饱",
        "stuffed": "撑得慌",
    },
    "energy": {
        "exhausted": "累垮了",
        "tired": "有点累",
        "normal": "还行",
        "rested": "精神不错",
        "charged": "精力充沛",
    },
}

_MASTER = "{MASTER_NAME}"
_LANLAN = "{LANLAN_NAME}"

_STAT_LABELS_ZH = {
    "affection": "与主人的关系",
    "mood": "心情",
    "health": "身体",
    "satiety": "肚子",
    "energy": "精神",
}
# 头部四行"当下状态"的展示顺序与标签（与 `core/model.STAT_NAMES` 的展示口径一致：
# 今天的任务在前、长期结果在后；好感单独一行，因为它不是"今天"的状态）
_BODY_LINES: tuple[tuple[str, str], ...] = (
    ("mood", "心情"),
    ("health", "身体"),
    ("satiety", "肚子"),
    ("energy", "精神"),
)

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

# 饱食与精力是"今天的状态"，与心情/身体这种"当下感受"分开写：
# 它们的变化有明确的因果对象（吃饭、睡觉），所以提示词里直接给出**该做什么**。
SATIETY_HINTS: dict[str, str] = {
    "starving": "你饿得有点发慌，注意力很难集中；可以直接说饿，但别演成惨兮兮的讨饭",
    "hungry": "你肚子有点空，会不自觉地提到吃的；语气可以带点软绵绵的撒娇",
    "satisfied": "你刚好吃饱，肚子是满足的状态",
    "full": "你吃得挺饱，懒洋洋的很舒服",
    "stuffed": "你吃得有点撑，动作会想慢一点",
}

ENERGY_HINTS: dict[str, str] = {
    "exhausted": "你累到不想动，回应会很短、很慢，甚至想直接去睡；别硬撑着热闹",
    "tired": "你有点困了，注意力不如平常，回应会短一些",
    "normal": "你的精力还够用",
    "rested": "你休息得不错，挺有精神的",
    "charged": "你现在精力很足，愿意多做点事、多聊几句",
}

# 档内程度词的收尾语气：低档是"很严重"，高档按轴取不同的正向词——
# 对饱食说"非常足"、对精力说"非常足"都别扭，所以按轴分开。
_INTENSITY_HIGH: dict[str, str] = {
    "mood": "非常足",
    "health": "非常足",
    "satiety": "撑得难受",
    "energy": "非常足",
}

_PHASE_LINES: dict[str, str] = {
    "morning": "现在是早上，你刚醒不久",
    "forenoon": "现在是上午",
    "noon": "现在是中午",
    "afternoon": "现在是下午",
    "evening": "现在是傍晚",
    "night": "现在是夜里",
    "late_night": "现在是深夜，你有点困了",
}

_HEADER = f"【{_LANLAN} 此刻的自身状态｜内部感知，供你调整语气与行为】"
_FOOTER = "以上只是你自己的感受，不要复述这段说明、不要提档位名称或任何数字、不要提及任何机制。"


def resolve_ai_behavior(trigger: str, stats: Stats, inject: InjectSettings) -> str:
    """决定这条注入是静默进上下文（read）还是让她主动开口（respond）。

    - `company`：工具路径，本来就是"她想找你"，总是主动开口。
    - `crisis` / `tier_change`：**只在真的处于危机档时**才升级为主动开口
      （`respond_on_crisis` 是总闸门）。v0.1.0 的语义是"危机 或 跨档都看她开不开口"，
     v0.2.0 收窄成"只有危机档才打断"——跨档（比如心情从平静跌到低落）进上下文就够了，
    否则她会在一天里反复主动开口，打扰感盖过了陪伴感。
    - `hungry` / `tired`：同样只在真的掉进危机档（饿坏了 / 累垮了）时才主动开口。
    - `anniversary` / `daily_greet` / `interval`：进上下文，不打断。
    - `staged_event`（v0.4.0）：**病愈**升级为主动开口——她刚从病里出来、正想说话，
      而且那是危机**解除**的通知，晚说就没有意义了；**哄好**则静默进上下文，
      免得"她心情回来了"变成一天里反复弹你的理由。
    """
    if trigger == TRIGGER_COMPANY:
        return "respond"
    if trigger == TRIGGER_STAGED_EVENT:
        return "read"
    if trigger == TRIGGER_CRISIS:
        return "respond" if inject.respond_on_crisis else "read"
    if trigger in (TRIGGER_TIER_CHANGE, TRIGGER_HUNGRY, TRIGGER_TIRED):
        if inject.respond_on_crisis and is_crisis(stats, inject):
            return "respond"
        return "read"
    return "read"


def build_text(
    *,
    stats: Stats,
    trigger: str,
    streak_days: int = 1,
    gap_hours: float | None = None,
    transitions: Iterable[tuple[str, str, str]] = (),
    max_chars: int = 320,
    rhythm: "DailyRhythm | None" = None,
    anniversary: "Anniversary | None" = None,
    day_number: int = 0,
    judgment_label: str = "",
    staged_event: "StagedEvent | None" = None,
) -> str:
    """装配注入正文。`transitions` 是 `core.model.tier_transitions` 的输出。

    `judgment_label` 只在 `trigger=TRIGGER_JUDGMENT` 时有意义（v0.3.0 反馈闭环）：
    她自己的判断以"一句感受"进上下文，**不带任何数字与档名**。
    `staged_event` 只在 `trigger=TRIGGER_STAGED_EVENT` 时有意义（v0.4.0 阶段性事件）：
    叙事读 `core/events` 的事件名，**同样不带数字与档名**——面板看得见数值，她不看。
    """
    required: list[str] = [_HEADER]

    for stat, label in _BODY_LINES:
        tier = tier_of(stat, getattr(stats, stat))
        required.append(
            f"　{label}：{TIER_LABELS_ZH[stat][tier]}（{_intensity_word(stat, getattr(stats, stat))}）"
        )
    affection_tier = tier_of("affection", stats.affection)
    required.append(
        "　与{master}的关系：{label}".format(
            master="{MASTER_NAME}", label=TIER_LABELS_ZH["affection"][affection_tier]
        )
    )

    optional: list[str] = []
    if rhythm is not None:
        phase_line = _PHASE_LINES.get(rhythm.phase, "")
        if phase_line:
            optional.append(f"　{phase_line}")
    if streak_days > 0:
        optional.append(f"　你们已经连续相处 {int(streak_days)} 天")
    if day_number > 0:
        optional.append(f"　从你们相遇算起，今天是第 {int(day_number)} 天")
    gap_line = _gap_line(gap_hours)
    if gap_line:
        optional.append(f"　{gap_line}")

    event_line = _event_line(
        trigger,
        transitions,
        anniversary,
        judgment_label=judgment_label,
        staged_event=staged_event,
    )
    if event_line:
        optional.append(f"　刚刚发生：{event_line}")

    tail = ["【现在该怎么表现】"]
    mood_tier = tier_of("mood", stats.mood)
    health_tier = tier_of("health", stats.health)
    satiety_tier = tier_of("satiety", stats.satiety)
    energy_tier = tier_of("energy", stats.energy)
    tail.extend(
        [
            MOOD_HINTS.get(mood_tier, ""),
            HEALTH_HINTS.get(health_tier, ""),
            SATIETY_HINTS.get(satiety_tier, ""),
            ENERGY_HINTS.get(energy_tier, ""),
            AFFECTION_HINTS.get(affection_tier, ""),
        ]
    )
    tail = [line for line in tail if line]

    return _assemble(required=required, optional=optional, tail=tail, max_chars=max(80, int(max_chars)))


def _assemble(*, required: Sequence[str], optional: Sequence[str], tail: Sequence[str], max_chars: int) -> str:
    """按优先级填充：头部与四项状态 > 行为倾向 > 相处细节 > 禁令。

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
    tiers = TIER_LABELS_ZH[stat]
    if index == 0:
        return "很严重"
    if index == len(tiers) - 1:
        return _INTENSITY_HIGH.get(stat, "非常足")
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


def _event_line(
    trigger: str,
    transitions: Iterable[tuple[str, str, str]],
    anniversary: "Anniversary | None" = None,
    *,
    judgment_label: str = "",
    staged_event: "StagedEvent | None" = None,
) -> str:
    if trigger == TRIGGER_STAGED_EVENT:
        # 阶段性事件：读事件名，不读数值。没有可讲的事件时**不兜底**成别的句子——
        # 这一档的存在意义就是"讲一件具体发生过的事"，讲不出来就不该走这条路
        # （`services/injector.plan_for_event` 只在真的拿到事件时才发这一档）。
        if staged_event is None:
            return ""
        return EVENT_NARRATION_ZH.get(staged_event.key, "")
    if trigger == TRIGGER_JUDGMENT:
        # 反馈闭环：她自己的判断。`label_line` 只给"一句感受"，没有数字与档名
        # （`neutral` 没有对应句子，返回空串 → 交回下面的兜底）。
        return label_line(judgment_label) or "你刚刚回味了一下和 {MASTER_NAME} 的这次相处"
    if trigger == TRIGGER_ANNIVERSARY:
        if anniversary is not None and anniversary.repeats_annually:
            return "今天是你们的周年纪念日"
        return "今天是你们的纪念日"
    if trigger == TRIGGER_HUNGRY:
        return "你的肚子空得让你没法专心"
    if trigger == TRIGGER_TIRED:
        return "你累到已经撑不住了"
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
        label = _STAT_LABELS_ZH.get(stat, TIER_LABELS_ZH.get(stat, {}).get(stat, stat))
        old_label = TIER_LABELS_ZH.get(stat, {}).get(old_tier, old_tier)
        new_label = TIER_LABELS_ZH.get(stat, {}).get(new_tier, new_tier)
        parts.append(f"{label}从「{old_label}」变成了「{new_label}」")
    return "；".join(parts)
