"""v0.2.0 数值模型门：饱食 / 精力的节律折算、跨轴耦合、吃饭、四轴危机。

与 `test_model.py`（v0.1.0 的三轴不变量）分开写：那份门是"不能被本次改动破坏"的
既有契约，这份门是"新增的两根轴与耦合到底怎么行为"的说明性门。
断言都写在行为层（"醒着掉得比睡着快"、"饿着心情掉更快"），而不是复述公式。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from our_life.core.configuration import DecaySettings, InjectSettings
from our_life.core.model import (
    COUPLING_FACTORS,
    COUPLING_THRESHOLDS,
    SATIETY_PER_HOUR_AWAKE,
    SATIETY_PER_HOUR_SLEEP,
    Stats,
    apply_coupling,
    apply_decay,
    apply_item,
    apply_meal,
    apply_neglect,
    apply_turn_gain,
    coupling_signal,
    crisis_axes,
    is_crisis,
    satiety_per_day,
    tier_of,
)
from our_life.core.rhythm import overlap_hours, resolve_rhythm

SLEEP_START = 24
SLEEP_END = 8


def _rhythm(hour: int, minute: int = 0) -> object:
    return resolve_rhythm(
        datetime(2026, 9, 14, hour, minute).timestamp(),
        sleep_start_hour=SLEEP_START,
        sleep_end_hour=SLEEP_END,
    )


# ---------------------------------------------------------------------------
# 两根新轴的默认值与向后兼容
# ---------------------------------------------------------------------------


def test_defaults_are_sane_and_readable() -> None:
    base = Stats()
    assert base.energy == pytest.approx(80.0)
    assert base.satiety == pytest.approx(70.0)
    assert tier_of("energy", base.energy) == "charged"
    assert tier_of("satiety", base.satiety) == "full"


def test_missing_keys_fall_back_to_defaults() -> None:
    """旧分片（v0.1.0）没有这两根轴：读进来应当是默认值，而不是 0 或报错。"""
    restored = Stats.from_mapping({"affection": 40.0, "mood": 30.0, "health": 50.0})
    assert restored.satiety == Stats().satiety
    assert restored.energy == Stats().energy


# ---------------------------------------------------------------------------
# 饱食：醒着掉得快、睡着掉得慢
# ---------------------------------------------------------------------------


def test_satiety_only_falls_and_never_rises_by_itself() -> None:
    decay = DecaySettings()
    after = apply_decay(Stats(satiety=100.0), elapsed_hours=12.0, decay=decay)
    assert after.satiety < 100.0
    # 她不会自己变饱：没有进食通道，只有衰减
    assert after.satiety <= 100.0


def test_awake_hours_burn_more_satiety_than_sleep_hours() -> None:
    decay = DecaySettings()
    awake = apply_decay(Stats(satiety=100.0), elapsed_hours=8.0, decay=decay, awake_hours=8.0)
    asleep = apply_decay(Stats(satiety=100.0), elapsed_hours=8.0, decay=decay, awake_hours=0.0)
    assert awake.satiety < asleep.satiety, "清醒时段应当掉得更快"
    expected = 8.0 * (SATIETY_PER_HOUR_AWAKE - SATIETY_PER_HOUR_SLEEP)
    assert asleep.satiety - awake.satiety == pytest.approx(expected, rel=1e-6)


def test_zero_elapsed_time_is_an_identity_even_when_hungry() -> None:
    """冻结拍（总开关关掉时每拍只推基准点）依赖这一条：0 小时必须什么都没变。"""
    hungry_and_tired = Stats(satiety=5.0, energy=5.0)
    assert apply_decay(hungry_and_tired, elapsed_hours=0.0, decay=DecaySettings()) == hungry_and_tired
    assert apply_coupling(hungry_and_tired, elapsed_hours=0.0, decay=DecaySettings()) == hungry_and_tired


def test_satiety_per_day_matches_the_integral_used_by_decay() -> None:
    """顾问面板的"每天几餐"与真实衰减必须同源，否则建议会偏。"""
    rhythm = _rhythm(12)
    per_day = satiety_per_day(rhythm=rhythm)
    assert per_day == pytest.approx(
        rhythm.wake_hours * SATIETY_PER_HOUR_AWAKE + rhythm.sleep_hours * SATIETY_PER_HOUR_SLEEP,
        rel=1e-9,
    )
    # 裸调用（没有作息）退化成"整天清醒"，与 v0.1.0 口径一致
    assert satiety_per_day() == pytest.approx(SATIETY_PER_HOUR_AWAKE * 24)


# ---------------------------------------------------------------------------
# 精力：睡眠窗回复、清醒消耗、深夜额外
# ---------------------------------------------------------------------------


def test_energy_drops_while_awake_and_recovers_in_sleep() -> None:
    decay = DecaySettings()
    awake = apply_decay(Stats(energy=80.0), elapsed_hours=8.0, decay=decay, awake_hours=8.0)
    asleep = apply_decay(Stats(energy=40.0), elapsed_hours=8.0, decay=decay, awake_hours=0.0)
    assert awake.energy < 80.0
    assert asleep.energy > 40.0


def test_a_normal_sleep_restores_energy_to_full() -> None:
    """默认作息（16 小时醒 / 8 小时睡）下，睡完一觉精力应当回到满。"""
    decay = DecaySettings()
    night = apply_decay(
        Stats(energy=65.0),
        elapsed_hours=8.0,
        decay=decay,
        awake_hours=0.0,
        rhythm=_rhythm(1),
    )
    assert night.energy == 100.0


def test_late_night_costs_more_energy() -> None:
    """同一个 1 小时，落在深夜（熬夜）比落在白天更耗精力。"""
    decay = DecaySettings()
    daytime = apply_decay(
        Stats(energy=80.0), elapsed_hours=1.0, decay=decay, awake_hours=1.0, rhythm=_rhythm(14)
    )
    late_night = apply_decay(
        Stats(energy=80.0), elapsed_hours=1.0, decay=decay, awake_hours=1.0, rhythm=_rhythm(23, 30)
    )
    assert late_night.energy < daytime.energy


def test_energy_never_goes_below_zero() -> None:
    drained = apply_decay(
        Stats(energy=1.0), elapsed_hours=48.0, decay=DecaySettings(), awake_hours=48.0
    )
    assert drained.energy == 0.0


def test_overlap_hours_agrees_with_the_rhythm_window() -> None:
    """`overlap_hours` 是服务层唯一读时钟的地方，它必须能把睡眠小时数算对。"""
    slept = overlap_hours(
        datetime(2026, 9, 14, 20, 0),
        datetime(2026, 9, 15, 8, 0),
        sleep_start_hour=SLEEP_START,
        sleep_end_hour=SLEEP_END,
    )
    assert slept == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# 跨轴耦合：饿 → 心情掉得更快；累 → 健康恢复更慢
# ---------------------------------------------------------------------------


def test_coupling_signal_is_neutral_above_the_thresholds() -> None:
    signal = coupling_signal(Stats(satiety=60.0, energy=60.0))
    assert signal.mood_factor == 1.0
    assert signal.health_factor == 1.0
    assert not signal.hungry and not signal.tired


def test_coupling_signal_stacks_at_the_severe_threshold() -> None:
    mild = coupling_signal(Stats(satiety=30.0, energy=30.0))
    severe = coupling_signal(Stats(satiety=10.0, energy=10.0))
    assert mild.mood_factor == pytest.approx(COUPLING_FACTORS["mood_from_satiety_low"])
    assert severe.mood_factor == pytest.approx(
        COUPLING_FACTORS["mood_from_satiety_low"] * COUPLING_FACTORS["mood_from_satiety_severe"]
    )
    assert severe.health_factor < mild.health_factor < 1.0


def test_hunger_makes_mood_fall_faster() -> None:
    decay = DecaySettings()
    fed = Stats(mood=90.0, satiety=90.0)
    hungry = Stats(mood=90.0, satiety=COUPLING_THRESHOLDS["satiety_low"] - 5.0)
    fed_after = apply_coupling(fed, elapsed_hours=1.0, decay=decay)
    hungry_after = apply_coupling(hungry, elapsed_hours=1.0, decay=decay)
    assert hungry_after.mood < fed_after.mood, "饿着的时候心情应当掉得更快"


def test_tiredness_slows_health_recovery() -> None:
    """累着时健康"朝基线恢复"变慢——所以低精力那一侧恢复得远不如正常那一侧快。

    两侧起点一致（health 40 < 基线 70，都会朝 70 回升），唯一的差别是精力：
    90 的按正常时间常数恢复，35 的被 ×0.385 压慢。这正是"熬夜攒久了会病"的机制。
    精力正常时 `apply_coupling` 在这根轴上**完全不动手**（不许把"没累"变成额外好处）。
    """
    decay = DecaySettings()
    rested = Stats(health=40.0, energy=90.0)
    tired = Stats(health=40.0, energy=COUPLING_THRESHOLDS["energy_low"] - 5.0)
    rested_after = apply_coupling(rested, elapsed_hours=6.0, decay=decay)
    tired_after = apply_coupling(tired, elapsed_hours=6.0, decay=decay)

    assert rested_after.health == rested.health, "精力正常时耦合不该动健康"
    assert tired_after.health > tired.health, "累着也会恢复，只是慢得多"
    assert tired_after.health < rested.health + 3.0, "被 ×0.385 压慢后不该追平正常恢复"


def test_decay_alone_carries_no_coupling() -> None:
    """`apply_decay` 只管时间；饿/累的影响只由 `apply_coupling` 施加。

    这条门是回归门：曾有一版在两个函数里都放了耦合，结果健康被恢复两次
    （低精力那侧反而比休息好的那侧恢复得更多），真机语义直接反过来。
    """
    decay = DecaySettings()
    drained = Stats(health=40.0, energy=35.0, mood=90.0, satiety=20.0)
    plain = apply_decay(drained, elapsed_hours=6.0, decay=decay)
    assert plain.health == pytest.approx(
        decay.health_rest_baseline + (40.0 - decay.health_rest_baseline) * 2.718281828 ** (-6.0 / 36.0),
        rel=1e-4,
    )
    assert plain.mood == pytest.approx(
        decay.mood_rest_baseline + (90.0 - decay.mood_rest_baseline) * 2.718281828 ** (-6.0 / 3.0),
        rel=1e-4,
    )


def test_coupling_does_not_touch_satiety_or_energy_itself() -> None:
    stats = Stats(satiety=5.0, energy=5.0, mood=50.0, health=50.0)
    after = apply_coupling(stats, elapsed_hours=3.0, decay=DecaySettings())
    assert after.satiety == stats.satiety
    assert after.energy == stats.energy


# ---------------------------------------------------------------------------
# 吃饭与用物品
# ---------------------------------------------------------------------------


def test_meal_restores_satiety_and_applies_item_effects() -> None:
    before = Stats(satiety=20.0, mood=50.0, health=50.0)
    after = apply_meal(before, satiety=38.0, effects={"satiety": 38.0, "mood": 3.0})
    assert after.satiety == pytest.approx(20.0 + 38.0 + 38.0)
    assert after.mood == pytest.approx(53.0)


def test_meal_is_clamped_at_the_ceiling() -> None:
    after = apply_meal(Stats(satiety=95.0), satiety=38.0)
    assert after.satiety == 100.0


def test_apply_item_ignores_unknown_keys() -> None:
    after = apply_item(Stats(mood=50.0), {"mood": 10.0, "not_a_stat": 999.0})
    assert after.mood == 60.0
    assert apply_item(Stats(), None) == Stats()
    assert apply_item(Stats(), {}) == Stats()


# ---------------------------------------------------------------------------
# 危机的四轴判据
# ---------------------------------------------------------------------------


def test_crisis_covers_all_four_axes() -> None:
    inject = InjectSettings()
    assert crisis_axes(Stats(mood=5.0), inject) == ("mood",)
    assert crisis_axes(Stats(health=5.0), inject) == ("health",)
    assert crisis_axes(Stats(satiety=5.0), inject) == ("satiety",)
    assert crisis_axes(Stats(energy=5.0), inject) == ("energy",)
    # 好感、以及"还不错"的两根轴都不算危机
    assert crisis_axes(Stats(affection=0.0, satiety=70.0, energy=80.0), inject) == ()
    assert is_crisis(Stats(mood=60.0, health=80.0, satiety=70.0, energy=80.0), inject) is False


def test_crisis_axes_are_reported_together() -> None:
    axes = crisis_axes(Stats(mood=5.0, energy=5.0), InjectSettings())
    assert set(axes) == {"mood", "energy"}


def test_hunger_tier_is_configured_independently() -> None:
    """把饱食危机档放宽到「有点饿」时，较高的档位也应被判为危机（"不高于"语义）。"""
    strict = InjectSettings()
    relaxed = InjectSettings(crisis_satiety_tier="hungry")
    assert not is_crisis(Stats(satiety=30.0), strict)
    assert is_crisis(Stats(satiety=30.0), relaxed)


# ---------------------------------------------------------------------------
# 旧门不受影响的地方（回归向）
# ---------------------------------------------------------------------------


def test_turn_gain_keeps_the_new_axes_untouched() -> None:
    """发言只长心情/健康/好感；饱食与精力不因为聊天变动。"""
    from our_life.core.configuration import GrowthSettings

    before = Stats(satiety=42.0, energy=33.0)
    after = apply_turn_gain(before, session_index=0, growth=GrowthSettings())
    assert after.satiety == before.satiety
    assert after.energy == before.energy


def test_neglect_keeps_the_new_axes_untouched() -> None:
    """冷落只扣心情/健康/好感——饱食与精力由作息驱动（再扣一次就是双重惩罚）。"""
    from our_life.core.configuration import NeglectSettings

    before = Stats(satiety=42.0, energy=33.0)
    after = apply_neglect(before, delta_days=3.0, neglect=NeglectSettings())
    assert after.satiety == before.satiety
    assert after.energy == before.energy
    assert after.mood < before.mood
