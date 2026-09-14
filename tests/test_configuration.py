"""配置韧性门。

配置来自 TOML 且用户会手改：类型漂移、缺键、负值、字符串数字都要能回退到默认值，
**绝不抛异常**——一个手滑的配置项不该让整条 tick 挂掉。
"""

from __future__ import annotations

from our_life.core.configuration import (
    MAX_TICK_SECONDS,
    MIN_TICK_SECONDS,
    DecaySettings,
    GrowthSettings,
    InjectSettings,
    NeglectSettings,
    OurLifeSettings,
)


def test_defaults_when_config_is_empty() -> None:
    settings = OurLifeSettings.from_config({})
    assert settings.enabled is False  # fail-closed
    assert settings.tick_seconds == 30
    assert settings.decay == DecaySettings()
    assert settings.growth == GrowthSettings()
    assert settings.neglect == NeglectSettings()
    assert settings.inject == InjectSettings()


def test_enabled_accepts_bool_and_common_string_forms() -> None:
    assert OurLifeSettings.from_config({"our_life": {"enabled": True}}).enabled is True
    assert OurLifeSettings.from_config({"our_life": {"enabled": "true"}}).enabled is True
    assert OurLifeSettings.from_config({"our_life": {"enabled": "off"}}).enabled is False
    assert OurLifeSettings.from_config({"our_life": {"enabled": "???"}}).enabled is False


def test_tick_seconds_is_clamped_into_sane_range() -> None:
    assert OurLifeSettings.from_config({"our_life": {"tick_seconds": 0}}).tick_seconds == MIN_TICK_SECONDS
    assert OurLifeSettings.from_config({"our_life": {"tick_seconds": 1}}).tick_seconds == MIN_TICK_SECONDS
    assert (
        OurLifeSettings.from_config({"our_life": {"tick_seconds": 10**9}}).tick_seconds == MAX_TICK_SECONDS
    )
    assert OurLifeSettings.from_config({"our_life": {"tick_seconds": "45"}}).tick_seconds == 45


def test_bad_types_fall_back_instead_of_raising() -> None:
    settings = OurLifeSettings.from_config(
        {
            "our_life": {
                "enabled": {"nested": "junk"},
                "tick_seconds": [1, 2, 3],
                "decay": "not-a-table",
                "growth": 5,
                "neglect": None,
                "inject": [],
            }
        }
    )
    assert settings.enabled is False
    assert settings.tick_seconds == 30
    assert settings.decay == DecaySettings()
    assert settings.growth == GrowthSettings()
    assert settings.neglect == NeglectSettings()
    assert settings.inject == InjectSettings()


def test_zero_or_negative_time_constants_are_replaced() -> None:
    decay = DecaySettings.from_mapping({"mood_tau_hours": 0, "health_tau_hours": -3.0, "affection_tau_days": 0})
    assert decay.mood_tau_hours > 0.0
    assert decay.health_tau_hours > 0.0
    assert decay.affection_tau_days > 0.0


def test_negative_penalties_are_flattened_to_zero() -> None:
    neglect = NeglectSettings.from_mapping(
        {"grace_hours": -1.0, "mood_penalty_per_day": -5.0, "health_penalty_cap": -2.0}
    )
    assert neglect.grace_hours == 0.0
    assert neglect.mood_penalty_per_day == 0.0
    assert neglect.health_penalty_cap == 0.0


def test_growth_lists_are_coerced_and_filtered() -> None:
    growth = GrowthSettings.from_mapping(
        {
            "streak_milestones": [3, "7", "junk", -1],
            "streak_affection_bonus": [5.0, "8.5", None],
        }
    )
    assert growth.streak_milestones == (3, 7)
    assert growth.streak_affection_bonus == (5.0, 8.5)


def test_growth_lists_fall_back_when_all_entries_invalid() -> None:
    growth = GrowthSettings.from_mapping({"streak_milestones": ["x"], "streak_mood_bonus": []})
    assert growth.streak_milestones == GrowthSettings().streak_milestones
    assert growth.streak_mood_bonus == GrowthSettings().streak_mood_bonus


def test_session_diminish_never_negative() -> None:
    assert GrowthSettings.from_mapping({"session_diminish": -1.0}).session_diminish == 0.0


def test_inject_limits_are_kept_usable() -> None:
    inject = InjectSettings.from_mapping({"max_per_hour": 0, "max_chars": 1, "min_interval_sec": -10})
    assert inject.max_per_hour >= 1
    assert inject.max_chars >= 80
    assert inject.min_interval_sec == 0.0


def test_inject_crisis_tiers_accept_strings() -> None:
    inject = InjectSettings.from_mapping({"crisis_mood_tier": "low", "crisis_health_tier": "frail"})
    assert inject.crisis_mood_tier == "low"
    assert inject.crisis_health_tier == "frail"


def test_section_lookup_survives_wrong_shapes() -> None:
    # our_life 是字符串而不是表时，不应崩，而是全默认
    settings = OurLifeSettings.from_config({"our_life": "oops"})
    assert settings == OurLifeSettings()
