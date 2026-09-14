"""注入文案门。

三条硬契约：
1. **不给模型看原始数字**，也不给档名标签（会被复述），所以文案里不能出现数值。
2. 必须带 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符（宿主在注入边界展开，插件无从得知称呼）。
3. 装配必须尊重 `max_chars`，且**任何分支都不能抛异常**——冷落分支曾经因为
   `"{MASTER_NAME} ...".format()` 把占位符当 format 字段而 KeyError，这里留了回归用例。
"""

from __future__ import annotations

import pytest
from our_life.core.configuration import InjectSettings
from our_life.core.injection import (
    TRIGGER_COMPANY,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_INTERVAL,
    TRIGGER_TIER_CHANGE,
    build_text,
    resolve_ai_behavior,
)
from our_life.core.model import Stats

ALL_TRIGGERS = (
    TRIGGER_TIER_CHANGE,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_INTERVAL,
    TRIGGER_COMPANY,
)


def test_text_never_leaks_raw_numbers() -> None:
    stats = Stats(affection=58.0, mood=72.0, health=48.0)
    text = build_text(stats=stats, trigger=TRIGGER_TIER_CHANGE, max_chars=600)
    for value in ("72", "48", "58"):
        assert value not in text


def test_text_keeps_role_placeholders() -> None:
    text = build_text(stats=Stats(), trigger=TRIGGER_INTERVAL, max_chars=600)
    assert "{MASTER_NAME}" in text
    assert "{LANLAN_NAME}" in text


def test_text_has_no_brace_format_artifacts() -> None:
    # 占位符必须原样保留，不能被 str.format 吃掉或替换成空
    text = build_text(stats=Stats(), trigger=TRIGGER_TIER_CHANGE, gap_hours=50.0, max_chars=800)
    assert "{" in text
    assert "MASTER_NAME}" in text
    assert "{}" not in text


@pytest.mark.parametrize("trigger", ALL_TRIGGERS)
def test_every_trigger_assembles_without_error(trigger: str) -> None:
    text = build_text(
        stats=Stats(affection=12.0, mood=8.0, health=8.0),
        trigger=trigger,
        streak_days=7,
        gap_hours=52.0,
        transitions=(("mood", "calm", "sulking"),),
        max_chars=320,
    )
    assert text
    assert len(text) <= 320


def test_gap_branches_are_all_reachable() -> None:
    """冷落分支回归：`{MASTER_NAME}` 不能被当成 format 字段（曾经在这里 KeyError）。"""
    short = build_text(stats=Stats(), trigger=TRIGGER_INTERVAL, gap_hours=0.5, max_chars=800)
    hours = build_text(stats=Stats(), trigger=TRIGGER_INTERVAL, gap_hours=5.0, max_chars=800)
    days = build_text(stats=Stats(), trigger=TRIGGER_INTERVAL, gap_hours=50.0, max_chars=800)
    assert "{MASTER_NAME}" in short
    assert "{MASTER_NAME}" in hours
    assert "{MASTER_NAME}" in days
    assert "天没来" in days


def test_missing_gap_source_omits_the_line() -> None:
    text = build_text(stats=Stats(), trigger=TRIGGER_INTERVAL, gap_hours=None, max_chars=800)
    assert "刚刚还在" not in text
    assert "没来" not in text


def test_text_respects_the_character_budget() -> None:
    text = build_text(
        stats=Stats(affection=95.0, mood=95.0, health=95.0),
        trigger=TRIGGER_DAILY_GREET,
        streak_days=365,
        gap_hours=30.0,
        transitions=(("mood", "calm", "elated"), ("health", "fair", "vigorous")),
        max_chars=200,
    )
    assert len(text) <= 200


def test_tiny_budget_still_returns_state_lines() -> None:
    text = build_text(stats=Stats(), trigger=TRIGGER_CRISIS, max_chars=1)
    assert "心情" in text  # 预算再小也保留三项状态，不退化成空串


def test_affection_tier_changes_the_tone_guidance() -> None:
    distant = build_text(stats=Stats(affection=5.0), trigger=TRIGGER_INTERVAL, max_chars=600)
    bonded = build_text(stats=Stats(affection=95.0), trigger=TRIGGER_INTERVAL, max_chars=600)
    assert distant != bonded


# ---------------------------------------------------------------------------
# ai_behavior 决策
# ---------------------------------------------------------------------------


def test_company_always_speaks_up() -> None:
    assert resolve_ai_behavior(TRIGGER_COMPANY, Stats(), InjectSettings()) == "respond"


def test_crisis_speaks_up_when_enabled() -> None:
    inject = InjectSettings(respond_on_crisis=True)
    assert resolve_ai_behavior(TRIGGER_CRISIS, Stats(mood=5.0), inject) == "respond"
    assert resolve_ai_behavior(TRIGGER_TIER_CHANGE, Stats(mood=5.0), inject) == "respond"


def test_non_crisis_tier_change_stays_quiet() -> None:
    inject = InjectSettings(respond_on_crisis=True)
    assert resolve_ai_behavior(TRIGGER_TIER_CHANGE, Stats(mood=70.0, health=80.0), inject) == "read"


def test_respond_on_crisis_can_be_turned_off() -> None:
    inject = InjectSettings(respond_on_crisis=False)
    assert resolve_ai_behavior(TRIGGER_CRISIS, Stats(mood=5.0), inject) == "read"


def test_daily_greet_and_interval_are_quiet() -> None:
    inject = InjectSettings()
    assert resolve_ai_behavior(TRIGGER_DAILY_GREET, Stats(), inject) == "read"
    assert resolve_ai_behavior(TRIGGER_INTERVAL, Stats(), inject) == "read"
