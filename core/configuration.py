"""配置视图：把 `[our_life]` 族配置表翻译成冻结的强类型设置对象。

为什么单独一层：core/ 纯函数只接受已规范化的数值参数，不接受原始 dict；
这样单测可以直接构造 `DecaySettings()`，不必伪造整份 TOML。

分档阈值**不在**这里：它们固定在 `core/model.py` 的 `TIER_BOUNDS`，作为单一来源，
避免"配置改了阈值但代码/文档没跟上"这类漂移（DESIGN.md 已声明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .coerce import as_bool, as_float, as_float_list, as_int, as_int_list, as_str, section

__all__ = [
    "DecaySettings",
    "GrowthSettings",
    "InjectSettings",
    "NeglectSettings",
    "OurLifeSettings",
    "MAX_TICK_SECONDS",
    "MIN_TICK_SECONDS",
]

# tick 间隔下限：太密会白烧 CPU；上限：太疏则惰性衰减的折算粒度变粗。
MIN_TICK_SECONDS = 5
MAX_TICK_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class DecaySettings:
    """时间常数与静息基线（越大掉得越慢）。"""

    mood_tau_hours: float = 3.0
    mood_rest_baseline: float = 55.0
    health_tau_hours: float = 36.0
    health_rest_baseline: float = 70.0
    affection_tau_days: float = 45.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "DecaySettings":
        base = cls()
        return cls(
            mood_tau_hours=_positive(as_float(raw.get("mood_tau_hours") if raw else None, base.mood_tau_hours)),
            mood_rest_baseline=as_float(
                raw.get("mood_rest_baseline") if raw else None, base.mood_rest_baseline
            ),
            health_tau_hours=_positive(
                as_float(raw.get("health_tau_hours") if raw else None, base.health_tau_hours)
            ),
            health_rest_baseline=as_float(
                raw.get("health_rest_baseline") if raw else None, base.health_rest_baseline
            ),
            affection_tau_days=_positive(
                as_float(raw.get("affection_tau_days") if raw else None, base.affection_tau_days)
            ),
        )


@dataclass(frozen=True, slots=True)
class GrowthSettings:
    """互动成长步长与里程碑奖励。"""

    turn_mood_gain: float = 2.5
    turn_health_gain: float = 0.8
    turn_affection_gain: float = 0.35
    session_diminish: float = 0.15
    day_greet_mood: float = 8.0
    day_greet_health: float = 2.0
    day_greet_affection: float = 3.0
    streak_milestones: tuple[int, ...] = (3, 7, 14, 30)
    streak_affection_bonus: tuple[float, ...] = (5.0, 8.0, 12.0, 20.0)
    streak_mood_bonus: tuple[float, ...] = (10.0, 10.0, 15.0, 20.0)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "GrowthSettings":
        base = cls()
        return cls(
            turn_mood_gain=as_float(raw.get("turn_mood_gain") if raw else None, base.turn_mood_gain),
            turn_health_gain=as_float(raw.get("turn_health_gain") if raw else None, base.turn_health_gain),
            turn_affection_gain=as_float(
                raw.get("turn_affection_gain") if raw else None, base.turn_affection_gain
            ),
            session_diminish=max(
                0.0, as_float(raw.get("session_diminish") if raw else None, base.session_diminish)
            ),
            day_greet_mood=as_float(raw.get("day_greet_mood") if raw else None, base.day_greet_mood),
            day_greet_health=as_float(raw.get("day_greet_health") if raw else None, base.day_greet_health),
            day_greet_affection=as_float(
                raw.get("day_greet_affection") if raw else None, base.day_greet_affection
            ),
            streak_milestones=as_int_list(
                raw.get("streak_milestones") if raw else None, base.streak_milestones
            ),
            streak_affection_bonus=as_float_list(
                raw.get("streak_affection_bonus") if raw else None, base.streak_affection_bonus
            ),
            streak_mood_bonus=as_float_list(
                raw.get("streak_mood_bonus") if raw else None, base.streak_mood_bonus
            ),
        )


@dataclass(frozen=True, slots=True)
class NeglectSettings:
    """冷落惩罚（拟真、按天、有上限）。"""

    grace_hours: float = 24.0
    mood_penalty_per_day: float = 6.0
    health_penalty_per_day: float = 4.0
    affection_penalty_per_day: float = 2.0
    mood_penalty_cap: float = 25.0
    health_penalty_cap: float = 20.0
    affection_penalty_cap: float = 12.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "NeglectSettings":
        base = cls()
        return cls(
            grace_hours=_non_negative(as_float(raw.get("grace_hours") if raw else None, base.grace_hours)),
            mood_penalty_per_day=_non_negative(
                as_float(raw.get("mood_penalty_per_day") if raw else None, base.mood_penalty_per_day)
            ),
            health_penalty_per_day=_non_negative(
                as_float(raw.get("health_penalty_per_day") if raw else None, base.health_penalty_per_day)
            ),
            affection_penalty_per_day=_non_negative(
                as_float(raw.get("affection_penalty_per_day") if raw else None, base.affection_penalty_per_day)
            ),
            mood_penalty_cap=_non_negative(
                as_float(raw.get("mood_penalty_cap") if raw else None, base.mood_penalty_cap)
            ),
            health_penalty_cap=_non_negative(
                as_float(raw.get("health_penalty_cap") if raw else None, base.health_penalty_cap)
            ),
            affection_penalty_cap=_non_negative(
                as_float(raw.get("affection_penalty_cap") if raw else None, base.affection_penalty_cap)
            ),
        )


@dataclass(frozen=True, slots=True)
class InjectSettings:
    """强注入的三重频控与危机升级。"""

    min_interval_sec: float = 1200.0
    max_per_hour: int = 3
    max_chars: int = 320
    respond_on_crisis: bool = True
    crisis_mood_tier: str = "sulking"
    crisis_health_tier: str = "sick"
    company_cooldown_sec: float = 7200.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "InjectSettings":
        base = cls()
        return cls(
            min_interval_sec=max(
                0.0, as_float(raw.get("min_interval_sec") if raw else None, base.min_interval_sec)
            ),
            max_per_hour=max(1, as_int(raw.get("max_per_hour") if raw else None, base.max_per_hour)),
            max_chars=max(80, as_int(raw.get("max_chars") if raw else None, base.max_chars)),
            respond_on_crisis=as_bool(
                raw.get("respond_on_crisis") if raw else None, base.respond_on_crisis
            ),
            crisis_mood_tier=as_str(raw.get("crisis_mood_tier") if raw else None, base.crisis_mood_tier),
            crisis_health_tier=as_str(
                raw.get("crisis_health_tier") if raw else None, base.crisis_health_tier
            ),
            company_cooldown_sec=max(
                0.0,
                as_float(raw.get("company_cooldown_sec") if raw else None, base.company_cooldown_sec),
            ),
        )


@dataclass(frozen=True, slots=True)
class OurLifeSettings:
    """`[our_life]` 根配置。"""

    enabled: bool = False
    tick_seconds: int = 30
    decay: DecaySettings = DecaySettings()
    growth: GrowthSettings = GrowthSettings()
    neglect: NeglectSettings = NeglectSettings()
    inject: InjectSettings = InjectSettings()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "OurLifeSettings":
        """从完整有效配置（`await self.config.dump()`）里取 `[our_life]` 族。"""
        root = section(config, "our_life")
        base = cls()
        return cls(
            enabled=as_bool(root.get("enabled"), base.enabled),
            tick_seconds=_clamp_tick(as_int(root.get("tick_seconds"), base.tick_seconds)),
            decay=DecaySettings.from_mapping(section(config, "our_life", "decay")),
            growth=GrowthSettings.from_mapping(section(config, "our_life", "growth")),
            neglect=NeglectSettings.from_mapping(section(config, "our_life", "neglect")),
            inject=InjectSettings.from_mapping(section(config, "our_life", "inject")),
        )


def _positive(value: float) -> float:
    return value if value > 0.0 else 1e-6


def _non_negative(value: float) -> float:
    return value if value > 0.0 else 0.0


def _clamp_tick(value: int) -> int:
    return max(MIN_TICK_SECONDS, min(MAX_TICK_SECONDS, value))
