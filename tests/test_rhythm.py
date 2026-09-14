"""作息节律门：时段、睡眠窗（含跨零点）、纪念日。

这一层决定"她的一天怎么过"（精力回复、饱食减速、深夜惩罚、纪念日触发），
所以断言写在**行为层**：几点算睡、跨零点的窗口覆盖哪些小时、相处第 N 天算不算纪念日。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from our_life.core.rhythm import (
    ANNIVERSARY_DAYS,
    anniversary_of,
    anniversary_of_day_number,
    day_number_of,
    days_until_next_anniversary,
    is_sleep_hour,
    minutes_until_next_boundary,
    normalize_hour,
    overlap_hours,
    phase_of,
    resolve_rhythm,
)

# ---------------------------------------------------------------------------
# 时段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "night"),
        (5, "morning"),
        (8, "morning"),
        (9, "forenoon"),
        (11, "forenoon"),
        (12, "noon"),
        (13, "noon"),
        (14, "afternoon"),
        (17, "afternoon"),
        (18, "evening"),
        (22, "evening"),
        (23, "late_night"),
    ],
)
def test_phase_boundaries(hour: int, expected: str) -> None:
    assert phase_of(hour) == expected


def test_phase_covers_every_hour_and_only_known_names() -> None:
    known = {"morning", "forenoon", "noon", "afternoon", "evening", "night", "late_night"}
    assert {phase_of(hour) for hour in range(24)} == known


def test_hour_normalization_accepts_24_as_end_of_day() -> None:
    assert normalize_hour(24) == 0
    assert normalize_hour(25) == 1
    assert normalize_hour(-1) == 23


# ---------------------------------------------------------------------------
# 睡眠窗
# ---------------------------------------------------------------------------


def test_sleep_window_spans_midnight() -> None:
    kwargs = {"sleep_start_hour": 24, "sleep_end_hour": 8}
    assert is_sleep_hour(0, **kwargs)
    assert is_sleep_hour(7, **kwargs)
    assert not is_sleep_hour(8, **kwargs)  # 半开区间：起床那一刻不算睡
    assert not is_sleep_hour(12, **kwargs)
    assert not is_sleep_hour(23, **kwargs)  # 24:00 才睡，23:00 还醒着


def test_sleep_window_inside_one_day() -> None:
    kwargs = {"sleep_start_hour": 13, "sleep_end_hour": 15}
    assert is_sleep_hour(13, **kwargs)
    assert is_sleep_hour(14, **kwargs)
    assert not is_sleep_hour(12, **kwargs)
    assert not is_sleep_hour(15, **kwargs)


def test_degenerate_sleep_window_means_never_asleep() -> None:
    """起止相同是配置写错：按"整天不睡"处理，绝不能变成"整天都在睡"。"""
    assert not any(is_sleep_hour(hour, sleep_start_hour=9, sleep_end_hour=9) for hour in range(24))


def test_rhythm_reports_windows_and_boundaries() -> None:
    moment = datetime(2026, 9, 14, 22, 30).timestamp()
    rhythm = resolve_rhythm(moment, sleep_start_hour=24, sleep_end_hour=8)
    assert rhythm.sleeping is False
    assert rhythm.sleep_hours == pytest.approx(8.0)
    assert rhythm.wake_hours == pytest.approx(16.0)
    assert rhythm.awake_ratio == pytest.approx(16 / 24, rel=1e-6)
    assert rhythm.hours_to_sleep == pytest.approx(1.5, rel=1e-3)
    assert rhythm.hours_to_wake == pytest.approx(9.5, rel=1e-3)
    assert rhythm.phase == "evening"
    assert minutes_until_next_boundary(moment, rhythm) == 90


def test_rhythm_while_asleep_counts_to_waking() -> None:
    moment = datetime(2026, 9, 14, 3, 0).timestamp()
    rhythm = resolve_rhythm(moment, sleep_start_hour=24, sleep_end_hour=8)
    assert rhythm.sleeping is True
    assert rhythm.phase == "night"
    assert minutes_until_next_boundary(moment, rhythm) == 300


def test_overlap_hours_counts_only_the_sleeping_part() -> None:
    start = datetime(2026, 9, 14, 20, 0)
    end = datetime(2026, 9, 15, 10, 0)
    slept = overlap_hours(start, end, sleep_start_hour=24, sleep_end_hour=8)
    # 20:00→10:00 共 14 小时，其中 00:00-08:00 睡着 → 8 小时
    assert slept == pytest.approx(8.0)


def test_overlap_hours_is_zero_for_an_empty_window() -> None:
    moment = datetime(2026, 9, 14, 12, 0)
    assert overlap_hours(moment, moment, sleep_start_hour=24, sleep_end_hour=8) == 0.0


# ---------------------------------------------------------------------------
# 相处天数与纪念日
# ---------------------------------------------------------------------------


def test_day_number_counts_the_first_day_as_one() -> None:
    assert day_number_of("2026-09-14", "2026-09-14") == 1
    assert day_number_of("2026-09-14", "2026-09-15") == 2
    assert day_number_of("2026-09-01", "2026-09-30") == 30


def test_day_number_survives_missing_or_broken_dates() -> None:
    assert day_number_of("", "2026-09-14") == 0
    assert day_number_of("2026-09-14", "") == 0
    assert day_number_of("not-a-date", "2026-09-14") == 0
    assert day_number_of("2026-09-20", "2026-09-14") == 0  # 未来日期不算


@pytest.mark.parametrize("day", [3, 7, 14, 30, 60, 100, 180, 365])
def test_milestone_days_are_anniversaries(day: int) -> None:
    assert anniversary_of_day_number(day) is not None


def test_ordinary_days_are_not_anniversaries() -> None:
    for day in (1, 2, 4, 6, 8, 29, 31, 99, 101):
        assert anniversary_of_day_number(day) is None


def test_yearly_anniversaries_repeat_after_the_last_milestone() -> None:
    # 一年口径是"相处满 365 天"，一年一遇、不重复触发（366 天不是纪念日，730 天是第二年）
    assert anniversary_of_day_number(366) is None
    second = anniversary_of_day_number(730)
    assert second is not None and second.repeats_annually and second.years == 2
    assert anniversary_of_day_number(1096) is None
    assert anniversary_of_day_number(0) is None


def test_anniversary_of_uses_the_first_day() -> None:
    assert anniversary_of("2026-09-14", "2026-09-16") is not None  # 第 3 天
    assert anniversary_of("2026-09-14", "2026-09-15") is None
    assert anniversary_of("", "2026-09-16") is None


def test_days_until_next_anniversary_hits_the_same_anchors() -> None:
    for day in range(1, 500):
        remaining = days_until_next_anniversary(day)
        assert remaining is not None and remaining > 0
        assert anniversary_of_day_number(day + remaining) is not None, (
            f"day {day} + {remaining} 不是纪念日——面板说还有 N 天，届时就该真的触发"
        )
    assert days_until_next_anniversary(2) == 1
    assert days_until_next_anniversary(0) == ANNIVERSARY_DAYS[0]
