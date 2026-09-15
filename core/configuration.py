"""配置视图：把 `[our_life]` 族配置表翻译成冻结的强类型设置对象。

为什么单独一层：core/ 纯函数只接受已规范化的数值参数，不接受原始 dict；
这样单测可以直接构造 `DecaySettings()`，不必伪造整份 TOML。

分档阈值与模型常量**不在**这里：它们固定在 `core/model.py`
（`TIER_BOUNDS` / `SATIETY_PER_HOUR_AWAKE` / `ENERGY_*` / 耦合阈值），作为单一来源，
避免"配置改了阈值但代码/文档没跟上"这类漂移（DESIGN.md 已声明）。
纪念日锚点同理固定在 `core/rhythm.py` 的 `ANNIVERSARY_DAYS`——面板显示的"还有 N 天"
与届时真的触发事件必须是同一个判据，所以不能让它变成可配置项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .coerce import as_bool, as_float, as_float_list, as_int, as_int_list, as_str, section

__all__ = [
    "CheckinSettings",
    "DecaySettings",
    "EconomySettings",
    "EventSettings",
    "FeedbackSettings",
    "GrowthSettings",
    "InjectSettings",
    "JobSettings",
    "NeglectSettings",
    "OurLifeSettings",
    "RhythmSettings",
    "MAX_HORIZON_DAYS",
    "MAX_TICK_SECONDS",
    "MIN_TICK_SECONDS",
]

# tick 间隔下限：太密会白烧 CPU；上限：太疏则惰性衰减的折算粒度变粗。
MIN_TICK_SECONDS = 5
MAX_TICK_SECONDS = 3600

# 口粮顾问的囤货视野上限（面板显示"建议囤到 N 天"，超过两周没有参考意义）
MAX_HORIZON_DAYS = 30


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
class RhythmSettings:
    """作息：睡眠窗 + 纪念日奖励。

    睡眠窗支持跨零点（`sleep_start_hour=24` 表示 24:00，等价于次日的 00:00）。
    起止相同视为"整天都不睡"（`core/rhythm.is_sleep_hour` 会处理这个退化配置），
    这样用户写错配置时她只是变成"永远清醒"，不会出现"整天都在睡觉"的荒谬状态。
    """

    sleep_start_hour: int = 24
    sleep_end_hour: int = 8
    anniversary_mood: float = 12.0
    anniversary_affection: float = 5.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RhythmSettings":
        base = cls()
        return cls(
            sleep_start_hour=_hour(as_int(raw.get("sleep_start_hour") if raw else None, base.sleep_start_hour)),
            sleep_end_hour=_hour(as_int(raw.get("sleep_end_hour") if raw else None, base.sleep_end_hour)),
            anniversary_mood=as_float(
                raw.get("anniversary_mood") if raw else None, base.anniversary_mood
            ),
            anniversary_affection=as_float(
                raw.get("anniversary_affection") if raw else None, base.anniversary_affection
            ),
        )


@dataclass(frozen=True, slots=True)
class EconomySettings:
    """金币经济：日薪、口粮种类、携带与顾问口径。

    `staple_item_id` 是"主人囤的那种口粮"——自动进食优先吃它、口粮顾问按它的单价
    算"补足建议"。物品表本身（卖什么、什么效果、多少钱）固定在 `core/economy.py`，
    是模型单一来源，不进配置。
    """

    enabled: bool = True
    staple_item_id: str = "meat"
    daily_allowance: int = 30
    carry_max: int = 8
    shop_daily_limit: int = 240
    meal_threshold: float = 55.0
    advisor_horizon_days: int = 7
    advisor_warn_days: float = 2.0
    start_sodas: int = 40
    turn_reward: float = 0.6
    new_day_bonus: float = 4.0
    streak_reward: float = 0.7
    max_turns_per_session: int = 20

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "EconomySettings":
        base = cls()
        return cls(
            enabled=as_bool(raw.get("enabled") if raw else None, base.enabled),
            staple_item_id=as_str(raw.get("staple_item_id") if raw else None, base.staple_item_id),
            daily_allowance=max(
                0, as_int(raw.get("daily_allowance") if raw else None, base.daily_allowance)
            ),
            carry_max=max(0, as_int(raw.get("carry_max") if raw else None, base.carry_max)),
            shop_daily_limit=max(
                0, as_int(raw.get("shop_daily_limit") if raw else None, base.shop_daily_limit)
            ),
            meal_threshold=_non_negative(
                as_float(raw.get("meal_threshold") if raw else None, base.meal_threshold)
            ),
            advisor_horizon_days=min(
                MAX_HORIZON_DAYS,
                max(1, as_int(raw.get("advisor_horizon_days") if raw else None, base.advisor_horizon_days)),
            ),
            advisor_warn_days=_non_negative(
                as_float(raw.get("advisor_warn_days") if raw else None, base.advisor_warn_days)
            ),
            start_sodas=max(0, as_int(raw.get("start_sodas") if raw else None, base.start_sodas)),
            turn_reward=_non_negative(
                as_float(raw.get("turn_reward") if raw else None, base.turn_reward)
            ),
            new_day_bonus=_non_negative(
                as_float(raw.get("new_day_bonus") if raw else None, base.new_day_bonus)
            ),
            streak_reward=_non_negative(
                as_float(raw.get("streak_reward") if raw else None, base.streak_reward)
            ),
            max_turns_per_session=max(
                1, as_int(raw.get("max_turns_per_session") if raw else None, base.max_turns_per_session)
            ),
        )


@dataclass(frozen=True, slots=True)
class CheckinSettings:
    """每日签到与补签（v0.7.0）。

    只暴露"数额与概率"旋钮；"连续天数怎么算"（集合后缀、含今天的奖励口径）
    是判据，固定在 `core/checkin.py`，不进配置——与分档阈值同一条纪律。
    """

    enabled: bool = True
    # 今天签到的基础金币；连续加成 = 每多连续一天 +streak_bonus_per_day（封顶见 cap）。
    base_coins: int = 8
    streak_bonus_per_day: int = 2
    # 加成封顶天数：第 11 天起不再增长（默认 8 + 2×10 = 28/天）。
    streak_cap_days: int = 10
    # 幸运事:命中概率与额外加成比例区间（结果四舍五入到整数金币）。
    luck_chance: float = 0.15
    luck_min_bonus: float = 0.5
    luck_max_bonus: float = 2.0
    # 补签：价格、可补窗口（天）、每 ISO 周额度。
    makeup_cost: int = 12
    makeup_window_days: int = 7
    makeup_week_limit: int = 1

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "CheckinSettings":
        base = cls()
        table = raw if isinstance(raw, Mapping) else {}
        chance = as_float(table.get("luck_chance"), base.luck_chance)
        low = _non_negative(as_float(table.get("luck_min_bonus"), base.luck_min_bonus))
        high = _non_negative(as_float(table.get("luck_max_bonus"), base.luck_max_bonus))
        if high < low:
            # 区间反写是手改坏的典型形状：退化为固定 min，而不是让掷骰子抛异常。
            high = low
        return cls(
            enabled=as_bool(table.get("enabled"), base.enabled),
            base_coins=max(0, as_int(table.get("base_coins"), base.base_coins)),
            streak_bonus_per_day=max(
                0, as_int(table.get("streak_bonus_per_day"), base.streak_bonus_per_day)
            ),
            streak_cap_days=max(0, as_int(table.get("streak_cap_days"), base.streak_cap_days)),
            luck_chance=max(0.0, min(1.0, chance)),
            luck_min_bonus=low,
            luck_max_bonus=high,
            makeup_cost=max(0, as_int(table.get("makeup_cost"), base.makeup_cost)),
            makeup_window_days=max(0, as_int(table.get("makeup_window_days"), base.makeup_window_days)),
            makeup_week_limit=max(0, as_int(table.get("makeup_week_limit"), base.makeup_week_limit)),
        )


@dataclass(frozen=True, slots=True)
class JobSettings:
    """猫娘打工（v0.7.0）：只暴露行为旋钮。

    工作目录（工时/工钱/损耗/门槛）固定在 `core/jobs.py` 作为单一来源，
    与商品表 `core/economy.ITEMS` 同一纪律——它们是"她怎么过日子"的刻画。
    """

    enabled: bool = True
    # 每日班次上限（代码天花板 `MAX_SHIFTS_PER_DAY` 会再夹一层）。
    max_shifts_per_day: int = 2
    # 早退折价：提前收工的工钱再乘这个比例（经兼现实：干一半的活拿不满的钱）。
    early_leave_ratio: float = 0.6

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "JobSettings":
        from .jobs import MAX_SHIFTS_PER_DAY

        base = cls()
        table = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=as_bool(table.get("enabled"), base.enabled),
            max_shifts_per_day=min(
                MAX_SHIFTS_PER_DAY,
                max(0, as_int(table.get("max_shifts_per_day"), base.max_shifts_per_day)),
            ),
            early_leave_ratio=max(
                0.0, min(1.0, as_float(table.get("early_leave_ratio"), base.early_leave_ratio))
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
    streak_sodas_bonus: tuple[int, ...] = (5, 10, 20, 40)

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
            streak_sodas_bonus=as_int_list(
                raw.get("streak_sodas_bonus") if raw else None, base.streak_sodas_bonus
            ),
        )


@dataclass(frozen=True, slots=True)
class FeedbackSettings:
    """反馈闭环：她自己的判断回流成数值修正的**四道旋钮**（v0.3.0）。

    这里只放"上限类"旋钮——**单次幅度是 `growth.turn_*_gain` 的引用**
    （`core/judgment._TURN_GAIN_SCALE`），不进配置：那条不变式是"她的判断压不过
    一次真实互动"，把它变成可配置项就等于允许用户把它调坏。

    两个日预算是正向/负向**分开**的：让工具能扣分等于给模型一条惩罚通道，
    所以负向的天花板默认只有正向的一半（4.0 vs 8.0），且同样需要真实互动作前提。
    """

    enabled: bool = True
    daily_add_points: float = 8.0
    daily_subtract_points: float = 4.0
    session_damp: float = 0.5

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "FeedbackSettings":
        base = cls()
        return cls(
            enabled=as_bool(raw.get("enabled") if raw else None, base.enabled),
            daily_add_points=_non_negative(
                as_float(raw.get("daily_add_points") if raw else None, base.daily_add_points)
            ),
            daily_subtract_points=_non_negative(
                as_float(
                    raw.get("daily_subtract_points") if raw else None, base.daily_subtract_points
                )
            ),
            session_damp=max(
                0.0, as_float(raw.get("session_damp") if raw else None, base.session_damp)
            ),
        )


@dataclass(frozen=True, slots=True)
class EventSettings:
    """阶段性事件（v0.4.0「阶段性事件与面板叙事」）。

    这里**只有开关与冷却**，没有阈值：两条"好起来了"的线（病愈 40、哄好 20）
    直接引用 `core/model.TIER_BOUNDS`，固定在 `core/events.py` 里作为单一来源。
    理由与"分档阈值不进配置"完全一样（见 `core/model.py` 与 DESIGN.md）——
    面板显示的档位与实际触发的事件必须是同一个判据，把它变成旋钮就等于允许
    用户把"什么算病好了"调得和面板显示的不一致。

    两个冷却是**时长**而不是"每天几次"：时长语义更直白（"六小时内不重复说同一件事"），
    而且不需要跨天重置的额外状态——台账本身就有界、直接在窗口里查即可。
    """

    enabled: bool = True
    # 心情是小时级（τ=3h），掉下去又上来是常态；6 小时压住反复，又允许一天里最多 4 次
    mood_recovery_min_interval_hours: float = 6.0
    # 比一天略短：允许"隔天又病了、又好了"这种真的两次经历，压住同一天里的抖动
    health_recovery_min_interval_hours: float = 20.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "EventSettings":
        base = cls()
        return cls(
            enabled=as_bool(raw.get("enabled") if raw else None, base.enabled),
            mood_recovery_min_interval_hours=_non_negative(
                as_float(
                    raw.get("mood_recovery_min_interval_hours") if raw else None,
                    base.mood_recovery_min_interval_hours,
                )
            ),
            health_recovery_min_interval_hours=_non_negative(
                as_float(
                    raw.get("health_recovery_min_interval_hours") if raw else None,
                    base.health_recovery_min_interval_hours,
                )
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
    """强注入的频控、危机升级与"她在睡觉"的静默窗。"""

    min_interval_sec: float = 1200.0
    max_per_hour: int = 3
    max_chars: int = 320
    respond_on_crisis: bool = True
    crisis_mood_tier: str = "sulking"
    crisis_health_tier: str = "sick"
    crisis_satiety_tier: str = "starving"
    crisis_energy_tier: str = "exhausted"
    company_cooldown_sec: float = 7200.0
    # `respond`（让她主动开口）单独的小时上限：比 `max_per_hour` 更严，
    # 否则"跨档 + 危机"叠加时会连着弹好几次主动开口（真机上很难受）
    respond_max_per_hour: int = 1
    # 她在睡觉时是否抑制**非危机**注入：凌晨三点把她叫醒说"我饿了"是反效果
    quiet_during_sleep: bool = True

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
            crisis_satiety_tier=as_str(
                raw.get("crisis_satiety_tier") if raw else None, base.crisis_satiety_tier
            ),
            crisis_energy_tier=as_str(
                raw.get("crisis_energy_tier") if raw else None, base.crisis_energy_tier
            ),
            company_cooldown_sec=max(
                0.0,
                as_float(raw.get("company_cooldown_sec") if raw else None, base.company_cooldown_sec),
            ),
            respond_max_per_hour=max(
                1, as_int(raw.get("respond_max_per_hour") if raw else None, base.respond_max_per_hour)
            ),
            quiet_during_sleep=as_bool(
                raw.get("quiet_during_sleep") if raw else None, base.quiet_during_sleep
            ),
        )


@dataclass(frozen=True, slots=True)
class OurLifeSettings:
    """`[our_life]` 根配置。"""

    enabled: bool = False
    tick_seconds: int = 30
    decay: DecaySettings = field(default_factory=DecaySettings)
    rhythm: RhythmSettings = field(default_factory=RhythmSettings)
    economy: EconomySettings = field(default_factory=EconomySettings)
    checkin: CheckinSettings = field(default_factory=CheckinSettings)
    job: JobSettings = field(default_factory=JobSettings)
    growth: GrowthSettings = field(default_factory=GrowthSettings)
    feedback: FeedbackSettings = field(default_factory=FeedbackSettings)
    events: EventSettings = field(default_factory=EventSettings)
    neglect: NeglectSettings = field(default_factory=NeglectSettings)
    inject: InjectSettings = field(default_factory=InjectSettings)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "OurLifeSettings":
        """从完整有效配置（`await self.config.dump()`）里取 `[our_life]` 族。"""
        root = section(config, "our_life")
        base = cls()
        return cls(
            enabled=as_bool(root.get("enabled"), base.enabled),
            tick_seconds=_clamp_tick(as_int(root.get("tick_seconds"), base.tick_seconds)),
            decay=DecaySettings.from_mapping(section(config, "our_life", "decay")),
            rhythm=RhythmSettings.from_mapping(section(config, "our_life", "rhythm")),
            economy=EconomySettings.from_mapping(section(config, "our_life", "economy")),
            checkin=CheckinSettings.from_mapping(section(config, "our_life", "checkin")),
            job=JobSettings.from_mapping(section(config, "our_life", "job")),
            growth=GrowthSettings.from_mapping(section(config, "our_life", "growth")),
            feedback=FeedbackSettings.from_mapping(section(config, "our_life", "feedback")),
            events=EventSettings.from_mapping(section(config, "our_life", "events")),
            neglect=NeglectSettings.from_mapping(section(config, "our_life", "neglect")),
            inject=InjectSettings.from_mapping(section(config, "our_life", "inject")),
        )


def _positive(value: float) -> float:
    return value if value > 0.0 else 1e-6


def _non_negative(value: float) -> float:
    return value if value > 0.0 else 0.0


def _hour(value: int) -> int:
    """把小时夹到 0..24（24 = 当天结束，等价于次日 00:00）。"""
    return max(0, min(24, int(value)))


def _clamp_tick(value: int) -> int:
    return max(MIN_TICK_SECONDS, min(MAX_TICK_SECONDS, value))
