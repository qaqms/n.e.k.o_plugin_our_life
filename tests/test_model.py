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
    Stats,
    advance_streak,
    apply_day_greet,
    apply_decay,
    apply_neglect,
    apply_streak_bonus,
    apply_turn_gain,
    clamp_value,
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
