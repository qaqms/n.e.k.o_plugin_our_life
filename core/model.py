"""数值模型：纯函数、零 SDK 依赖。

三根轴（均 0..100）：
- `mood`    心情：小时级，向静息基线回落（高于基线降、低于基线回升，homeostasis）
- `health`  健康：天级，同样向静息基线回落；长期冷落额外扣
- `affection` 好感：周级，**只缓慢回落、不自动回升**——它只能靠互动长

衰减是「读数时惰性折算」：`exp(-Δt/τ)` 把存储值折算到当前时刻，只在写入时落盘。
这样既不依赖常驻循环，也不怕进程重启，更避开了 timer 每拍新建 event loop 的陷阱。

分档阈值固定在 `TIER_BOUNDS`（单一来源，不进配置），档名是稳定 ASCII 键：
面向用户的档名走 i18n（`panel.tier.<stat>.<tier>`），面向模型的档名走 `core/injection.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import exp
from typing import Any, Iterable, Mapping

from .configuration import DecaySettings, GrowthSettings, InjectSettings, NeglectSettings

__all__ = [
    "AFFECTION_TIERS",
    "HEALTH_TIERS",
    "MAX_VALUE",
    "MIN_VALUE",
    "MOOD_TIERS",
    "STAT_NAMES",
    "TIER_BOUNDS",
    "Stats",
    "advance_streak",
    "apply_day_greet",
    "apply_decay",
    "apply_neglect",
    "apply_streak_bonus",
    "apply_turn_gain",
    "clamp_value",
    "is_crisis",
    "neglect_entitlement_days",
    "streak_milestone_bonus",
    "tier_index_of",
    "tier_of",
    "tier_transitions",
]

MIN_VALUE = 0.0
MAX_VALUE = 100.0

# 分档下界（升序）：0-19 / 20-39 / 40-59 / 60-79 / 80-100
TIER_BOUNDS = (0.0, 20.0, 40.0, 60.0, 80.0)

AFFECTION_TIERS = ("stranger", "acquainted", "close", "intimate", "bonded")
MOOD_TIERS = ("sulking", "low", "calm", "happy", "elated")
HEALTH_TIERS = ("sick", "frail", "fair", "good", "vigorous")

STAT_NAMES = ("affection", "mood", "health")

_TIERS: dict[str, tuple[str, ...]] = {
    "affection": AFFECTION_TIERS,
    "mood": MOOD_TIERS,
    "health": HEALTH_TIERS,
}


@dataclass(frozen=True, slots=True)
class Stats:
    """一个角色卡的三项数值（未夹取；需要时调 `clamped()`）。"""

    affection: float = 20.0
    mood: float = 55.0
    health: float = 70.0

    def clamped(self) -> "Stats":
        return Stats(
            affection=clamp_value(self.affection),
            mood=clamp_value(self.mood),
            health=clamp_value(self.health),
        )

    def as_dict(self) -> dict[str, float]:
        return {"affection": self.affection, "mood": self.mood, "health": self.health}

    def tier_map(self) -> dict[str, str]:
        return {name: tier_of(name, getattr(self, name)) for name in STAT_NAMES}

    def with_value(self, stat: str, value: float) -> "Stats":
        data = self.as_dict()
        data[stat] = clamp_value(value)
        return Stats(**data)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Stats":
        """从持久化数据恢复；缺键/非法值一律回退默认（损坏数据不该让插件起不来）。"""
        base = cls()
        if not isinstance(raw, Mapping):
            return base
        out: dict[str, float] = {}
        for name in STAT_NAMES:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                out[name] = getattr(base, name)
            else:
                out[name] = clamp_value(float(value))
        return cls(**out)


def clamp_value(value: float) -> float:
    if value != value:  # NaN
        return MIN_VALUE
    return max(MIN_VALUE, min(MAX_VALUE, float(value)))


def tier_index_of(stat: str, value: float) -> int:
    """返回档位下标（0 最低）。未知 stat 抛 KeyError（调用方只应传 STAT_NAMES）。"""
    tiers = _TIERS[stat]
    bounded = clamp_value(value)
    index = 0
    for position, lower in enumerate(TIER_BOUNDS):
        if bounded >= lower:
            index = position
    return min(index, len(tiers) - 1)


def tier_of(stat: str, value: float) -> str:
    return _TIERS[stat][tier_index_of(stat, value)]


def tier_transitions(before: Stats, after: Stats) -> tuple[tuple[str, str, str], ...]:
    """哪些轴的档位发生了跨越（含升级与降级），返回 (轴, 旧档, 新档)。"""
    out: list[tuple[str, str, str]] = []
    for name in STAT_NAMES:
        old_tier = tier_of(name, getattr(before, name))
        new_tier = tier_of(name, getattr(after, name))
        if old_tier != new_tier:
            out.append((name, old_tier, new_tier))
    return tuple(out)


def is_crisis(stats: Stats, inject: InjectSettings) -> bool:
    """是否掉进危机档（心情/健康任一"不高于"配置档位）。

    用「不高于」而不是「等于」：配置把阈值指到更高档时语义自然扩展，
    也不会因为一次跨越两档而漏判。
    """
    mood_index = tier_index_of("mood", stats.mood)
    health_index = tier_index_of("health", stats.health)
    crisis_mood = _tier_index("mood", inject.crisis_mood_tier)
    crisis_health = _tier_index("health", inject.crisis_health_tier)
    return mood_index <= crisis_mood or health_index <= crisis_health


def _tier_index(stat: str, tier: str) -> int:
    try:
        return _TIERS[stat].index(tier)
    except ValueError:
        # 配置写了未知档名：退回最低档（最保守口径 = 尽量不误报危机）
        return 0


# ---------------------------------------------------------------------------
# 衰减
# ---------------------------------------------------------------------------


def _decay_toward_baseline(current: float, *, tau_hours: float, baseline: float, hours: float) -> float:
    if hours <= 0.0:
        return current
    ratio = exp(-hours / tau_hours)
    return baseline + (current - baseline) * ratio


def _decay_toward_zero(current: float, *, tau_hours: float, hours: float) -> float:
    if hours <= 0.0:
        return current
    return current * exp(-hours / tau_hours)


def apply_decay(stats: Stats, *, elapsed_hours: float, decay: DecaySettings) -> Stats:
    """按真实流逝时间折算三项数值（不落盘；调用方决定何时持久化）。"""
    if elapsed_hours <= 0.0:
        return stats
    return Stats(
        affection=clamp_value(
            _decay_toward_zero(
                stats.affection, tau_hours=decay.affection_tau_days * 24.0, hours=elapsed_hours
            )
        ),
        mood=clamp_value(
            _decay_toward_baseline(
                stats.mood,
                tau_hours=decay.mood_tau_hours,
                baseline=decay.mood_rest_baseline,
                hours=elapsed_hours,
            )
        ),
        health=clamp_value(
            _decay_toward_baseline(
                stats.health,
                tau_hours=decay.health_tau_hours,
                baseline=decay.health_rest_baseline,
                hours=elapsed_hours,
            )
        ),
    )


# ---------------------------------------------------------------------------
# 成长 / 冷落
# ---------------------------------------------------------------------------


def apply_turn_gain(stats: Stats, *, session_index: int, growth: GrowthSettings) -> Stats:
    """一次用户发言的成长。`session_index` 是本次会话内第几次（0 起），用于递减收益。"""
    damp = 1.0 / (1.0 + max(0, session_index) * growth.session_diminish)
    return Stats(
        affection=clamp_value(stats.affection + growth.turn_affection_gain * damp),
        mood=clamp_value(stats.mood + growth.turn_mood_gain * damp),
        health=clamp_value(stats.health + growth.turn_health_gain * damp),
    )


def apply_day_greet(stats: Stats, growth: GrowthSettings) -> Stats:
    """跨天首次互动的一次性礼物。"""
    return Stats(
        affection=clamp_value(stats.affection + growth.day_greet_affection),
        mood=clamp_value(stats.mood + growth.day_greet_mood),
        health=clamp_value(stats.health + growth.day_greet_health),
    )


def streak_milestone_bonus(
    streak_days: int, *, awarded: Iterable[int], growth: GrowthSettings
) -> tuple[int, ...]:
    """返回本次新达成的里程碑（未在 `awarded` 里且 streak 已越过），保持升序。"""
    awarded_set = {int(x) for x in awarded}
    fresh: list[int] = []
    for milestone in growth.streak_milestones:
        if streak_days >= milestone and milestone not in awarded_set:
            fresh.append(milestone)
    return tuple(sorted(fresh))


def apply_streak_bonus(
    stats: Stats, *, milestones: Iterable[int], growth: GrowthSettings
) -> Stats:
    """给若干里程碑的累计奖励（里程碑配置与奖励表按位对应，越界则忽略该位）。"""
    affection = stats.affection
    mood = stats.mood
    for milestone in milestones:
        try:
            position = growth.streak_milestones.index(milestone)
        except ValueError:
            continue
        if position < len(growth.streak_affection_bonus):
            affection += growth.streak_affection_bonus[position]
        if position < len(growth.streak_mood_bonus):
            mood += growth.streak_mood_bonus[position]
    return Stats(
        affection=clamp_value(affection),
        mood=clamp_value(mood),
        health=clamp_value(stats.health),
    )


def neglect_entitlement_days(gap_hours: float, *, grace_hours: float) -> float:
    """冷落应扣天数 = 超出宽限时长的天数（不足一天按比例）。"""
    if gap_hours <= grace_hours:
        return 0.0
    return (gap_hours - grace_hours) / 24.0


def apply_neglect(stats: Stats, *, delta_days: float, neglect: NeglectSettings) -> Stats:
    """按**增量**天数扣分（调用方用已结算天数算出增量，保证幂等）。

    各项单独夹在上限内，避免"一个月没来直接清零"这种不可逆体验。
    """
    if delta_days <= 0.0:
        return stats
    mood_penalty = min(delta_days * neglect.mood_penalty_per_day, neglect.mood_penalty_cap)
    health_penalty = min(delta_days * neglect.health_penalty_per_day, neglect.health_penalty_cap)
    affection_penalty = min(
        delta_days * neglect.affection_penalty_per_day, neglect.affection_penalty_cap
    )
    return Stats(
        affection=clamp_value(stats.affection - affection_penalty),
        mood=clamp_value(stats.mood - mood_penalty),
        health=clamp_value(stats.health - health_penalty),
    )


def advance_streak(
    last_active_date: str | None, today: str, current_streak: int
) -> tuple[int, bool, bool]:
    """把连续互动天数结算到 `today`。

    返回 `(新的 streak、是否今天首次互动、是否断档)`：

    - `last_active_date == today` → 今天已经来过：streak 原样，`is_new_day=False`
      （跨天礼物不会重复给）
    - `last_active_date == today 的前一天` → streak + 1，`is_new_day=True`
    - 其它（更早 / 为空 / 非法日期）→ streak 重置为 1，`is_new_day=True`，断档标记依据是否曾有记录
    """
    if not today:
        return max(1, current_streak), False, False
    if last_active_date == today:
        return max(1, current_streak), False, False
    if _previous_day(today) == last_active_date:
        return max(1, current_streak) + 1, True, False
    return 1, True, bool(last_active_date)


def _previous_day(day: str) -> str | None:
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return None
    return (parsed - timedelta(days=1)).isoformat()
