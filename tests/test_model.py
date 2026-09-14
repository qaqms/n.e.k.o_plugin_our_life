"""数值模型门：分档、衰减、成长、冷落、连续天数、危机判定。

这些是插件最核心的不变量——数值算错会直接导致"她莫名奇妙闹脾气"，
所以断言写在行为层（"高于基线要降、低于基线要回升"），而不是复述实现公式。
"""

from __future__ import annotations

import pytest
from our_life.core.configuration import DecaySettings, GrowthSettings, InjectSettings, NeglectSettings
from our_life.core.model import (
    AFFECTION_TIERS,
    HEALTH_TIERS,
    MOOD_TIERS,
    STAT_NAMES,
    TIER_BOUNDS,
    TIER_CROSSING_MARGIN,
    Stats,
    advance_streak,
    apply_day_greet,
    apply_decay,
    apply_neglect,
    apply_streak_bonus,
    apply_turn_gain,
    clamp_value,
    crosses_tier_boundary,
    eventful_tier_transitions,
    is_crisis,
    neglect_entitlement_days,
    streak_milestone_bonus,
    tier_index_of,
    tier_of,
    tier_transitions,
)

# ---------------------------------------------------------------------------
# 分档
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stat", "value", "expected"),
    [
        ("affection", 0.0, "stranger"),
        ("affection", 19.9, "stranger"),
        ("affection", 20.0, "acquainted"),
        ("affection", 59.9, "close"),
        ("affection", 100.0, "bonded"),
        ("mood", 0.0, "sulking"),
        ("mood", 20.0, "low"),
        ("mood", 40.0, "calm"),
        ("mood", 60.0, "happy"),
        ("mood", 80.0, "elated"),
        ("health", 19.0, "sick"),
        ("health", 40.0, "fair"),
        ("health", 79.0, "good"),
        ("health", 80.0, "vigorous"),
    ],
)
def test_tier_boundaries(stat: str, value: float, expected: str) -> None:
    assert tier_of(stat, value) == expected


def test_tier_tables_are_five_and_aligned_with_bounds() -> None:
    assert len(AFFECTION_TIERS) == len(MOOD_TIERS) == len(HEALTH_TIERS) == 5
    for stat in STAT_NAMES:
        assert tier_index_of(stat, 0.0) == 0
        assert tier_index_of(stat, 100.0) == 4


def test_clamp_handles_out_of_range_and_nan() -> None:
    assert clamp_value(-5.0) == 0.0
    assert clamp_value(180.0) == 100.0
    assert clamp_value(float("nan")) == 0.0


# ---------------------------------------------------------------------------
# 惰性衰减
# ---------------------------------------------------------------------------


def test_decay_is_noop_without_elapsed_time() -> None:
    stats = Stats(affection=50.0, mood=80.0, health=30.0)
    assert apply_decay(stats, elapsed_hours=0.0, decay=DecaySettings()) == stats


def test_mood_above_baseline_falls_and_below_baseline_rises() -> None:
    decay = DecaySettings()
    high = apply_decay(Stats(mood=100.0), elapsed_hours=decay.mood_tau_hours, decay=decay)
    low = apply_decay(Stats(mood=10.0), elapsed_hours=decay.mood_tau_hours, decay=decay)
    baseline = decay.mood_rest_baseline
    assert baseline < high.mood < 100.0
    assert baseline > low.mood > 10.0
    # 一个时间常数后应该走完约 63%
    assert high.mood == pytest.approx(baseline + (100.0 - baseline) * 0.36787944, rel=1e-4)


def test_affection_only_decays_toward_zero() -> None:
    decay = DecaySettings()
    hours = decay.affection_tau_days * 24.0
    after = apply_decay(Stats(affection=100.0), elapsed_hours=hours, decay=decay)
    assert 0.0 < after.affection < 40.0
    # 好感不会"自动回升"：基线 0 意味着低于 0 无意义，长时间只会趋近 0
    long_after = apply_decay(Stats(affection=100.0), elapsed_hours=hours * 10, decay=decay)
    assert long_after.affection < after.affection
    assert long_after.affection >= 0.0


def test_health_decays_toward_its_baseline() -> None:
    decay = DecaySettings()
    after = apply_decay(Stats(health=100.0), elapsed_hours=decay.health_tau_hours, decay=decay)
    assert decay.health_rest_baseline < after.health < 100.0


# ---------------------------------------------------------------------------
# 成长 / 冷落
# ---------------------------------------------------------------------------


def test_turn_gain_is_positive_and_diminishes_within_one_session() -> None:
    growth = GrowthSettings()
    first = apply_turn_gain(Stats(), session_index=0, growth=growth)
    fifth = apply_turn_gain(Stats(), session_index=4, growth=growth)
    assert first.mood > Stats().mood
    assert first.mood - Stats().mood == pytest.approx(growth.turn_mood_gain)
    assert fifth.mood - Stats().mood < first.mood - Stats().mood


def test_day_greet_gives_more_than_a_single_turn() -> None:
    growth = GrowthSettings()
    greet = apply_day_greet(Stats(), growth)
    turn = apply_turn_gain(Stats(), session_index=0, growth=growth)
    assert greet.affection > turn.affection
    assert greet.mood > turn.mood


def test_streak_milestones_only_award_unawarded_ones() -> None:
    growth = GrowthSettings()
    fresh = streak_milestone_bonus(7, awarded=(), growth=growth)
    assert fresh == (3, 7)
    assert streak_milestone_bonus(7, awarded=(3, 7), growth=growth) == ()
    assert streak_milestone_bonus(30, awarded=(3, 7), growth=growth) == (14, 30)


def test_streak_bonus_is_clamped_at_100() -> None:
    grown = apply_streak_bonus(Stats(affection=99.0, mood=99.0), milestones=(3, 7, 14, 30), growth=GrowthSettings())
    assert grown.affection == 100.0
    assert grown.mood == 100.0


def test_neglect_entitlement_respects_grace_window() -> None:
    assert neglect_entitlement_days(10.0, grace_hours=24.0) == 0.0
    assert neglect_entitlement_days(48.0, grace_hours=24.0) == pytest.approx(1.0)


def test_neglect_penalties_are_capped_per_axis() -> None:
    neglect = NeglectSettings()
    punished = apply_neglect(Stats(affection=90.0, mood=90.0, health=90.0), delta_days=100.0, neglect=neglect)
    assert punished.mood == pytest.approx(90.0 - neglect.mood_penalty_cap)
    assert punished.health == pytest.approx(90.0 - neglect.health_penalty_cap)
    assert punished.affection == pytest.approx(90.0 - neglect.affection_penalty_cap)


def test_neglect_with_zero_delta_is_noop() -> None:
    stats = Stats()
    assert apply_neglect(stats, delta_days=0.0, neglect=NeglectSettings()) == stats


# ---------------------------------------------------------------------------
# 连续天数
# ---------------------------------------------------------------------------


def test_advance_streak_same_day_does_not_double_count() -> None:
    assert advance_streak("2026-09-14", "2026-09-14", 4) == (4, False, False)


def test_advance_streak_consecutive_day_increments() -> None:
    assert advance_streak("2026-09-13", "2026-09-14", 4) == (5, True, False)


def test_advance_streak_gap_resets_to_one_and_reports_break() -> None:
    assert advance_streak("2026-09-10", "2026-09-14", 9) == (1, True, True)


def test_advance_streak_without_history_starts_at_one() -> None:
    assert advance_streak("", "2026-09-14", 0) == (1, True, False)
    assert advance_streak(None, "2026-09-14", 3) == (1, True, False)


def test_advance_streak_survives_corrupt_date() -> None:
    streak, is_new_day, broken = advance_streak("not-a-date", "2026-09-14", 2)
    assert (streak, is_new_day, broken) == (1, True, True)


# ---------------------------------------------------------------------------
# 危机判定与跨档
# ---------------------------------------------------------------------------


def test_crisis_triggers_on_low_mood_or_low_health() -> None:
    inject = InjectSettings()
    assert is_crisis(Stats(mood=10.0), inject) is True
    assert is_crisis(Stats(health=5.0), inject) is True
    assert is_crisis(Stats(affection=5.0), inject) is False  # 好感低不算"需要被照顾"
    assert is_crisis(Stats(mood=60.0, health=80.0), inject) is False


def test_crisis_uses_at_or_below_semantics() -> None:
    # 配置把危机档放宽到 calm/fair 时，更高的档位也应被判为危机（不因一次跨两档而漏判）
    inject = InjectSettings(crisis_mood_tier="calm", crisis_health_tier="fair")
    assert is_crisis(Stats(mood=45.0), inject) is True


def test_tier_transitions_detects_crossings_only() -> None:
    before = Stats(mood=59.0, health=70.0, affection=20.0)
    after = Stats(mood=61.0, health=70.0, affection=19.0)
    transitions = tier_transitions(before, after)
    assert ("mood", "calm", "happy") in transitions
    assert all(item[0] != "health" for item in transitions)
    assert tier_transitions(before, before) == ()


# ---------------------------------------------------------------------------
# 分界噪声（真机 store 里抓到的那一类）
# ---------------------------------------------------------------------------
#
# 病因：分档线是 `value >= lower` 的硬比较，而默认好评 20.0 **正好压在**
# stranger/acquainted 的线上。落一个 30 秒心跳，`apply_decay` 就把它折成 19.9998456796，
# 于是"跨档"成立、`tier_change` 强注入立刻发出——面板上数值还是 20.0，用户看不到任何变化。
# 这一组门盯的就是"注入判据不许被亚分噪声触发，但真变化照旧触发"。


def test_default_affection_sits_exactly_on_a_tier_boundary() -> None:
    """前提门：这条噪声不是巧合，默认值就落在分界线上。

    哪天有人把默认值挪开了，本组门里"真机复现"那条会失去意义——所以把前提也钉住，
    让挪默认值的人顺手看到这里。
    """
    default_affection = Stats().affection
    assert default_affection in TIER_BOUNDS
    assert tier_of("affection", default_affection) != tier_of("affection", default_affection - 1e-6)


def test_crosses_tier_boundary_ignores_noise_but_keeps_real_crossings() -> None:
    # 噪声：0.0002 分的翻档（真机 store 里逐字抓到的前后两点）
    assert not crosses_tier_boundary("affection", before=20.0, after=19.9998456796)
    assert not crosses_tier_boundary("affection", before=20.0001, after=19.9999)
    # 噪声：两点都紧贴同一条分界线、只是分处两侧（会来回抖的那种）
    assert not crosses_tier_boundary("affection", before=19.96, after=20.04)
    assert not crosses_tier_boundary("mood", before=60.03, after=59.97)
    # 非跨档：同一档内部的往复
    assert not crosses_tier_boundary("mood", before=25.0, after=23.0)
    # 真变化：离分界线远得多，档位确实变了（面板上的档名也变了）
    assert crosses_tier_boundary("affection", before=20.0, after=19.0)
    assert crosses_tier_boundary("affection", before=19.9, after=20.2)
    assert crosses_tier_boundary("mood", before=59.9, after=61.0)
    assert crosses_tier_boundary("health", before=31.0, after=18.0)
    # 一次跨两档：离中间那条线必然很远，照样判出来
    assert crosses_tier_boundary("mood", before=39.0, after=61.0)


def test_hysteresis_is_bounded_by_the_margin() -> None:
    """迟滞不能变成"永远不判跨档"：离开分界线超过余量就必须判出来。"""
    for lower in TIER_BOUNDS[1:]:
        assert crosses_tier_boundary(
            "affection", before=lower, after=lower - TIER_CROSSING_MARGIN - 0.01
        ), f"越界 {lower} 超过余量仍未判为跨档"
        assert crosses_tier_boundary(
            "affection", before=lower - 1.0, after=lower + 1.0
        ), f"跨越 {lower} 一分以上仍未判为跨档"


def test_real_machine_defect_is_gone() -> None:
    """真机复现门：把 store 里那对数值喂进两条判据，硬比较会红、事件判据必须干净。

    这条门就是把"曾经在真机 store 里发生过的一次错误注入"钉在墙上——
    修复前 `eventful_tier_transitions` 会返回一条 `tier_change`，注入随即发出。
    """
    decay = DecaySettings()
    before = Stats()
    after = apply_decay(before, elapsed_hours=30.0 / 3600.0, decay=decay)

    # 前提：这一拍确实把值折过了分界线（否则本条门白测）
    assert tier_transitions(before, after) != ()
    assert tier_of("affection", before.affection) != tier_of("affection", after.affection)

    # 但注入判据必须认为"什么都没发生"
    assert eventful_tier_transitions(before, after) == ()
    assert not crosses_tier_boundary(
        "affection", before=before.affection, after=after.affection
    )


def test_eventful_transitions_keeps_only_the_real_crossing() -> None:
    # 心情真的跨了一档（59→61 是 calm→happy）；好感只是被衰减推过分界线一点点
    before = Stats(affection=20.0, mood=59.0, health=70.0)
    after = Stats(affection=19.9998456796, mood=61.0, health=70.0)
    assert eventful_tier_transitions(before, after) == (("mood", "calm", "happy"),)


def test_eventful_transitions_reuses_the_plain_tier_names() -> None:
    before = Stats(affection=20.0, mood=59.0, health=70.0)
    after = Stats(affection=19.0, mood=61.0, health=70.0)
    eventful = eventful_tier_transitions(before, after)
    # 顺序按 `STAT_NAMES`（精力/饱食在最前），所以这里比集合而不是比序列
    assert set(eventful) == {("affection", "acquainted", "stranger"), ("mood", "calm", "happy")}
    for stat, old_tier, new_tier in eventful:
        assert old_tier == tier_of(stat, getattr(before, stat))
        assert new_tier == tier_of(stat, getattr(after, stat))
