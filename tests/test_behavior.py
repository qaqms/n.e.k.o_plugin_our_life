"""行为聚合门：从只读总线记录里挑出"属于某个角色卡的新互动"。

这里最重要的一条不变量是**归属隔离**：别的角色卡的轮次绝不能被算成本角色的互动，
而且不能被标记为"已见"（否则切回那个角色时会漏算）。
"""

from __future__ import annotations

from our_life.core.behavior import (
    gap_hours,
    local_day,
    normalize_turn,
    select_new_turns,
    summarize_turns,
)


def _record(conversation_id: str, timestamp: float, lanlan: str, turn_type: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "timestamp": timestamp,
        "metadata": {"lanlan_name": lanlan, "turn_type": turn_type},
    }


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def test_normalize_reads_role_and_turn_type_from_metadata() -> None:
    turn = normalize_turn(_record("c1", 1000.0, "灵", "user"))
    assert turn is not None
    assert (turn.conversation_id, turn.lanlan_name, turn.turn_type) == ("c1", "灵", "user")
    assert turn.is_user is True


def test_normalize_drops_records_without_id_or_timestamp() -> None:
    assert normalize_turn({"timestamp": 1.0}) is None
    assert normalize_turn({"conversation_id": "c1"}) is None
    assert normalize_turn(None) is None


def test_normalize_falls_back_to_record_level_role() -> None:
    turn = normalize_turn({"conversation_id": "c9", "timestamp": 5.0, "lanlan_name": "喵"}, fallback_lanlan="X")
    assert turn is not None
    assert turn.lanlan_name == "喵"
    # 类型缺失时不能假设是用户轮次
    assert turn.is_user is False


def test_normalize_uses_fallback_role_when_record_has_none() -> None:
    turn = normalize_turn({"conversation_id": "c9", "timestamp": 5.0}, fallback_lanlan="灵")
    assert turn is not None
    assert turn.lanlan_name == "灵"


# ---------------------------------------------------------------------------
# 归属与去重
# ---------------------------------------------------------------------------


def test_select_new_turns_filters_other_characters_without_marking_them_seen() -> None:
    records = (
        _record("a1", 100.0, "灵", "user"),
        _record("b1", 200.0, "雪", "user"),
    )
    fresh, fresh_ids = select_new_turns(records, lanlan="灵", seen_ids=())
    assert [turn.conversation_id for turn in fresh] == ["a1"]
    assert fresh_ids == ("a1",)
    assert "b1" not in fresh_ids  # 别家角色卡的轮次不算"已消费"

    # 切到另一个角色时，那条记录仍然能被算上
    fresh_other, _ = select_new_turns(records, lanlan="雪", seen_ids=())
    assert [turn.conversation_id for turn in fresh_other] == ["b1"]


def test_select_new_turns_skips_already_seen_ids() -> None:
    records = (_record("a1", 100.0, "灵", "user"), _record("a2", 200.0, "灵", "user"))
    fresh, fresh_ids = select_new_turns(records, lanlan="灵", seen_ids=("a1",))
    assert [turn.conversation_id for turn in fresh] == ["a2"]
    assert fresh_ids == ("a2",)


def test_select_new_turns_returns_chronological_order() -> None:
    records = (_record("late", 300.0, "灵", "user"), _record("early", 100.0, "灵", "user"))
    fresh, _ = select_new_turns(records, lanlan="灵", seen_ids=())
    assert [turn.conversation_id for turn in fresh] == ["early", "late"]


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def test_summarize_counts_user_turns_only() -> None:
    records = (
        _record("u1", 100.0, "灵", "user"),
        _record("a1", 150.0, "灵", "assistant"),
        _record("u2", 200.0, "灵", "user"),
    )
    fresh, _ = select_new_turns(records, lanlan="灵", seen_ids=())
    summary = summarize_turns(fresh)
    assert summary.user_turns == 2
    assert summary.assistant_turns == 1
    assert summary.last_user_at == 200.0
    assert summary.first_user_at == 100.0


def test_summarize_without_user_turns_reports_no_touch() -> None:
    records = (_record("a1", 100.0, "灵", "assistant"),)
    fresh, _ = select_new_turns(records, lanlan="灵", seen_ids=())
    summary = summarize_turns(fresh)
    assert summary.user_turns == 0
    assert summary.last_user_at is None


def test_summarize_hour_histogram_tracks_local_hours() -> None:
    records = (_record("u1", 100.0, "灵", "user"),)
    fresh, _ = select_new_turns(records, lanlan="灵", seen_ids=())
    summary = summarize_turns(fresh)
    assert sum(summary.hour_histogram) == 1
    assert len(summary.hour_histogram) == 24


def test_summarize_on_empty_input_is_all_zero() -> None:
    summary = summarize_turns(())
    assert summary.user_turns == 0
    assert summary.conversation_ids == ()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def test_gap_hours_never_negative() -> None:
    # "现在"早于"更早那个时刻"（时钟回拨/坏数据）时必须钳到 0，不能变成负的空档
    assert gap_hours(400.0, 1000.0) == 0.0
    assert gap_hours(4000.0, 400.0) == 1.0
    assert gap_hours(4000.0, None) == 0.0


def test_local_day_matches_local_calendar() -> None:
    assert local_day(0.0) == local_day(0.0)
    assert len(local_day(1_700_000_000.0)) == 10


def test_local_day_degrades_on_corrupt_input() -> None:
    assert local_day(float("nan")) == ""
