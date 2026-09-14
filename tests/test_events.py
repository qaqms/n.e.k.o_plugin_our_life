"""阶段性事件门（v0.4.0）。

这一层是**纯函数**，所以断言写在行为层：什么算"她病好了"、什么不算、
什么事只该说一次、睡过去的事算不算发生过。不去复述实现里的比较运算符。

四类不变量（顺序与重要程度一致）：

1. **判据线 == 档位线**：病愈看健康 40、哄好看心情 20。谁把 `TIER_BOUNDS` 或
   事件里的线改得不一样，这里立刻红——因为否则会出现"面板显示她还病着，
   插件却认为她好了"这种自相矛盾。
2. **只认向上跨过，且只跨一次**：向下掉、停在线上、以及抖动（19.99 → 20.01）
   的语义边界要明确。
3. **冷却**：同一件事在窗口内不重复。跨过窗口就能再次发生（"隔天又病了又好了"
   是两次真实经历，不该被永久压掉）。
4. **睡眠只影响"说不说"**：`pick_event` 永远返回事件，睡眠抑制在注入层
   （`services.injector.wake_suppressed`）——这两件事分开是刻意的，见 `core/events` 第 4 条。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from our_life.core.configuration import EventSettings, InjectSettings, OurLifeSettings
from our_life.core.events import (
    EVENT_CHEERED_UP,
    EVENT_KEYS,
    EVENT_SICK_RECOVERY,
    HEALTH_RECOVERY_LINE,
    MOOD_RECOVERY_LINE,
    StagedEvent,
    event_already_fired,
    pick_event,
)
from our_life.core.model import STAT_NAMES, TIER_BOUNDS, Stats, tier_index_of, tier_of
from our_life.services.injector import recovery_line_for, wake_suppressed
from our_life.services.state import EVENT_HISTORY_MAX, ShardState

HOUR = 3600.0
NOW = 1_700_000_000.0


def _stats(**overrides: float) -> Stats:
    """基准数值刻意都落在安全区，避免"改一个轴"的测试被别的轴带跑。"""
    base = {"mood": 55.0, "health": 70.0, "satiety": 60.0, "energy": 60.0, "affection": 20.0}
    base.update(overrides)
    return Stats(**base)


def _pick(before: Stats, after: Stats, *, ledger=(), settings: EventSettings | None = None):
    return pick_event(
        before=before,
        after=after,
        settings=settings or EventSettings(),
        now=NOW,
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# 1. 判据线与档位线同源
# ---------------------------------------------------------------------------


def test_lines_are_the_tier_boundaries_they_claim_to_be() -> None:
    """病愈线 = 「生病 / 虚弱」的上界 = 40；哄好线 = 「闹脾气」的上界 = 20。

    断言方式刻意用 `tier_index_of` 反查：这样即使 `TIER_BOUNDS` 整体平移，
    只要"线落在哪个档位上"没变，这里仍然成立（而"线 == 某个具体数字"会假红）。
    """
    assert HEALTH_RECOVERY_LINE == TIER_BOUNDS[2], "病愈线必须等于 sick/frail 两档的上界"
    assert MOOD_RECOVERY_LINE == TIER_BOUNDS[1], "哄好线必须等于 sulking 档的上界"

    # 线下一点点仍然是"病着 / 闹脾气"，线上一点点已经不是
    assert tier_of("health", HEALTH_RECOVERY_LINE - 0.01) in ("sick", "frail")
    assert tier_of("health", HEALTH_RECOVERY_LINE) not in ("sick", "frail")
    assert tier_of("mood", MOOD_RECOVERY_LINE - 0.01) == "sulking"
    assert tier_of("mood", MOOD_RECOVERY_LINE) != "sulking"


def test_event_keys_are_stable_ascii_and_unique() -> None:
    """事件名要能直接拼 i18n 键（`panel.event.<键>`），所以必须是稳定 ASCII 且不重复。"""
    assert len(set(EVENT_KEYS)) == len(EVENT_KEYS)
    for key in EVENT_KEYS:
        assert key.replace("_", "").isalnum() and key.islower(), f"事件名必须是 snake_case ASCII: {key!r}"


# ---------------------------------------------------------------------------
# 2. 只认向上跨过
# ---------------------------------------------------------------------------


def test_health_crossing_the_sick_ceiling_fires() -> None:
    event = _pick(_stats(health=39.0), _stats(health=41.0))
    assert event is not None
    assert event.key == EVENT_SICK_RECOVERY
    assert event.stat == "health"
    assert event.wake_ok is True, "病愈是危机解除通知，必须允许在睡觉时也说"
    assert event.width == pytest.approx(1.0)


def test_health_stopping_exactly_on_the_line_counts_as_recovered() -> None:
    """面板显示 40.0 时档位就已经是「一般」了，判据必须跟着它（闭区间）。"""
    event = _pick(_stats(health=39.5), _stats(health=HEALTH_RECOVERY_LINE))
    assert event is not None and event.key == EVENT_SICK_RECOVERY
    assert event.width == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (40.0, 60.0),  # 早就在线上（一直停在线上不重复触发）
        (60.0, 80.0),  # 线上继续上升
        (60.0, 30.0),  # 向下掉（恶化不是"病愈"）
        (30.0, 35.0),  # 在生病档内回升但没跨出去
        (39.99, 39.999),  # 贴着线但没过去
    ],
)
def test_health_does_not_fire_in_these_cases(before: float, after: float) -> None:
    assert _pick(_stats(health=before), _stats(health=after)) is None


def test_mood_crossing_the_sulking_ceiling_fires_but_does_not_wake_her() -> None:
    event = _pick(_stats(mood=19.0), _stats(mood=25.0))
    assert event is not None
    assert event.key == EVENT_CHEERED_UP
    assert event.stat == "mood"
    assert event.wake_ok is False, "哄好是好消息，不该在睡觉时吵醒她"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (20.0, 30.0),  # 早就不在闹脾气档
        (25.0, 40.0),  # 从「低落」到「平静」——刻意不算事件（词会贬值）
        (40.0, 15.0),  # 向下掉进闹脾气
        (15.0, 18.0),  # 档内回升没跨出去
    ],
)
def test_mood_does_not_fire_in_these_cases(before: float, after: float) -> None:
    assert _pick(_stats(mood=before), _stats(mood=after)) is None


def test_both_crossings_in_the_same_tick_pick_the_heavier_one() -> None:
    """同拍两件事都发生 → 只报更重的那件（病愈）。

    心情回升几乎总跟着病愈一起发生，所以这条不是理论情况。
    """
    event = _pick(_stats(health=39.0, mood=15.0), _stats(health=45.0, mood=30.0))
    assert event is not None and event.key == EVENT_SICK_RECOVERY


def test_turning_events_off_stops_detection_entirely() -> None:
    assert _pick(_stats(health=39.0), _stats(health=45.0), settings=EventSettings(enabled=False)) is None


# ---------------------------------------------------------------------------
# 3. 冷却
# ---------------------------------------------------------------------------


def test_same_event_does_not_repeat_inside_its_cooldown() -> None:
    settings = EventSettings(mood_recovery_min_interval_hours=6.0)
    ledger = [{"key": EVENT_CHEERED_UP, "at": NOW - 2 * HOUR, "stat": "mood"}]
    assert _pick(_stats(mood=10.0), _stats(mood=30.0), ledger=ledger, settings=settings) is None

    # 窗口外就能再次发生
    ledger_outside = [{"key": EVENT_CHEERED_UP, "at": NOW - 7 * HOUR, "stat": "mood"}]
    again = _pick(_stats(mood=10.0), _stats(mood=30.0), ledger=ledger_outside, settings=settings)
    assert again is not None and again.key == EVENT_CHEERED_UP


def test_cooldown_is_per_event_not_global() -> None:
    """病愈的冷却**不该**压住哄好——它们是两件事，各有各的窗口。"""
    settings = EventSettings()
    ledger = [{"key": EVENT_SICK_RECOVERY, "at": NOW - 1 * HOUR, "stat": "health"}]
    event = _pick(_stats(mood=10.0), _stats(mood=30.0), ledger=ledger, settings=settings)
    assert event is not None and event.key == EVENT_CHEERED_UP


def test_health_recovery_window_is_longer_than_the_mood_one() -> None:
    """病愈的默认冷却比一天略短（允许隔天再病一次），哄好是 6 小时。"""
    settings = EventSettings()
    assert settings.health_recovery_min_interval_hours >= 20.0
    assert settings.health_recovery_min_interval_hours < 24.0
    assert settings.mood_recovery_min_interval_hours < settings.health_recovery_min_interval_hours


def test_zero_cooldown_is_allowed_and_means_always() -> None:
    settings = EventSettings(mood_recovery_min_interval_hours=0.0)
    ledger = [{"key": EVENT_CHEERED_UP, "at": NOW, "stat": "mood"}]
    assert _pick(_stats(mood=10.0), _stats(mood=30.0), ledger=ledger, settings=settings) is not None


def test_ledger_garbage_never_raises_and_never_suppresses() -> None:
    """脏台账（旧版分片、手改过的 store、截断的 JSON）不许让判定炸掉。

    一切不可识别的条目按"不构成冷却证据"处理：宁可多说一次，也不能因为
    一条坏数据让她永远说不出"我好起来了"。
    """
    junk = [
        None,
        "not-a-mapping",
        {"key": 123, "at": "yesterday"},
        {"at": NOW},  # 没有 key
        {"key": EVENT_CHEERED_UP},  # 没有 at
        {"key": EVENT_CHEERED_UP, "at": "soon"},
        {"key": EVENT_CHEERED_UP, "at": float("nan")},
    ]
    event = _pick(_stats(mood=10.0), _stats(mood=30.0), ledger=junk)  # type: ignore[arg-type]
    assert event is not None and event.key == EVENT_CHEERED_UP


def test_event_already_fired_reads_the_ledger_without_touching_cooldown() -> None:
    ledger = [{"key": EVENT_SICK_RECOVERY, "at": NOW - 3 * HOUR}]
    assert event_already_fired(key=EVENT_SICK_RECOVERY, ledger=ledger, now=NOW, within_seconds=24 * HOUR)
    assert not event_already_fired(key=EVENT_SICK_RECOVERY, ledger=ledger, now=NOW, within_seconds=1 * HOUR)
    assert not event_already_fired(key=EVENT_CHEERED_UP, ledger=ledger, now=NOW, within_seconds=24 * HOUR)
    assert not event_already_fired(key=EVENT_SICK_RECOVERY, ledger=ledger, now=NOW, within_seconds=0.0)


# ---------------------------------------------------------------------------
# 4. 睡眠分家：识别 vs 注入
# ---------------------------------------------------------------------------


def test_sleeping_does_not_change_detection() -> None:
    """`pick_event` 不接睡眠参数——"算不算发生过"与"要不要说"是两个问题。

    这条门存在的意义是**防回归**：有人一旦把睡眠判断塞进 `core/events`，
    睡过去的事件就不会被记账，下一拍会被重新识别一遍（冷却靠台账）。
    """
    event = _pick(_stats(mood=10.0), _stats(mood=30.0))
    assert event is not None and event.wake_ok is False
    assert "sleeping" not in pick_event.__code__.co_varnames, (
        "core/events 不该知道睡眠——睡眠抑制在 services/injector.wake_suppressed"
    )


class _Rhythm:
    """最小作息替身：`wake_suppressed` 只读 `sleeping` 一个字段。"""

    def __init__(self, *, sleeping: bool) -> None:
        self.sleeping = sleeping


@pytest.mark.parametrize(
    ("key", "sleeping", "quiet", "expected"),
    [
        # 病愈（wake_ok=True）：睡觉也照说——那是危机解除
        (EVENT_SICK_RECOVERY, True, True, False),
        (EVENT_SICK_RECOVERY, False, True, False),
        # 哄好（wake_ok=False）：睡觉时不说
        (EVENT_CHEERED_UP, True, True, True),
        (EVENT_CHEERED_UP, False, True, False),
        # 用户把睡觉静默关掉：两件都说
        (EVENT_CHEERED_UP, True, False, False),
        # 拿不到作息快照（rhythm=None）：不抑制，宁可说
        (EVENT_CHEERED_UP, False, True, False),
    ],
)
def test_wake_suppression_rules(key: str, sleeping: bool, quiet: bool, expected: bool) -> None:
    event = StagedEvent(key=key, stat="health", value=45.0, width=5.0, wake_ok=key == EVENT_SICK_RECOVERY)
    settings = replace(OurLifeSettings(), inject=replace(InjectSettings(), quiet_during_sleep=quiet))
    rhythm = _Rhythm(sleeping=sleeping) if sleeping else None
    assert wake_suppressed(event=event, settings=settings, rhythm=rhythm) is expected


# ---------------------------------------------------------------------------
# 收尾：台账条目形状（面板与 store 都吃它）
# ---------------------------------------------------------------------------


def test_event_payload_is_json_safe_and_carries_no_free_text() -> None:
    """台账条目必须是 JSON 安全的，且**只**含事件名 / 轴 / 时刻 / 数值——不含任何正文。"""
    event = StagedEvent(key=EVENT_CHEERED_UP, stat="mood", value=25.123456, width=5.123456, wake_ok=False)
    payload = event.as_dict(at=NOW)
    assert json.loads(json.dumps(payload)) == payload  # JSON 安全
    assert set(payload) == {"key", "stat", "value", "width", "at"}
    assert payload["key"] == EVENT_CHEERED_UP
    assert payload["value"] == 25.12  # 两位小数，与面板口径一致
    assert payload["width"] == 5.12


def test_recovery_line_for_matches_the_events_own_line() -> None:
    """`recovery_line_for` 只用于把跨越写成"从哪一档到哪一档"，必须与事件同源。"""
    sick = StagedEvent(key=EVENT_SICK_RECOVERY, stat="health", value=41.0, width=1.0, wake_ok=True)
    cheered = StagedEvent(key=EVENT_CHEERED_UP, stat="mood", value=25.0, width=5.0, wake_ok=False)
    assert recovery_line_for(sick) == HEALTH_RECOVERY_LINE
    assert recovery_line_for(cheered) == MOOD_RECOVERY_LINE
    # 未知事件退化成"从当前值减去跨越幅度"，不抛
    unknown = StagedEvent(key="some_future_event", stat="health", value=50.0, width=3.0, wake_ok=False)
    assert recovery_line_for(unknown) == 47.0


# ---------------------------------------------------------------------------
# 台账落盘往返（schema 4）
# ---------------------------------------------------------------------------


def test_shard_state_roundtrips_the_event_ledger() -> None:
    state = ShardState(lanlan="测试角色")
    for index in range(3):
        state.note_event(
            {"key": EVENT_CHEERED_UP, "stat": "mood", "value": 25.0 + index, "width": 5.0, "at": NOW + index}
        )
    restored = ShardState.from_payload("测试角色", state.as_payload(), now=NOW)
    assert len(restored.event_history) == 3
    assert restored.event_history[-1]["value"] == 27.0
    assert restored.recent_events(limit=2)[0]["at"] == NOW + 2, "最近的排在最前"


def test_event_ledger_is_bounded_and_drops_unknown_keys() -> None:
    state = ShardState(lanlan="测试角色")
    for index in range(EVENT_HISTORY_MAX + 5):
        state.note_event({"key": EVENT_CHEERED_UP, "stat": "mood", "at": NOW + index})
    assert len(state.event_history) == EVENT_HISTORY_MAX
    assert state.event_history[-1]["at"] == NOW + EVENT_HISTORY_MAX + 4

    # 形状不对的条目不进台账（见 ShardState.note_event：只吸收已知键）
    state = ShardState(lanlan="测试角色")
    state.note_event({"key": "", "stat": "mood", "at": NOW})
    state.note_event({"key": EVENT_CHEERED_UP, "stat": "", "at": NOW})
    state.note_event({"key": EVENT_CHEERED_UP, "stat": "mood"})
    state.note_event({"key": EVENT_CHEERED_UP, "stat": "mood", "at": "now"})
    state.note_event({"key": EVENT_CHEERED_UP, "stat": "mood", "at": NOW, "extra": "ignored"})
    assert len(state.event_history) == 1
    assert set(state.event_history[0]) == {"key", "stat", "at"}


def test_old_shard_without_event_ledger_still_loads() -> None:
    """v3 分片（没有 `event_history` 键）读进来必须是空台账，而不是报错。

    真机上一定有这种分片：用户从 v0.3.0 升上来，store 里那份是旧的。
    """
    legacy = {
        "schema_version": 3,
        "stats": {"mood": 50.0, "health": 60.0, "satiety": 50.0, "energy": 50.0, "affection": 30.0},
        "last_decay_at": NOW,
        "judgment_history": [{"at": NOW, "label": "good", "applied": 1.0}],
    }
    state = ShardState.from_payload("测试角色", legacy, now=NOW)
    assert state.event_history == ()
    assert len(state.judgment_history) == 1, "旧台账仍要读进来"
    assert set(STAT_NAMES) == {"mood", "health", "satiety", "energy", "affection"}


def test_panel_snapshot_exposes_events_without_free_text() -> None:
    """面板快照里的 `events` 块只能带台账字段——它会被送到前端，不能夹带正文。"""
    state = ShardState(lanlan="测试角色")
    state.note_event({"key": EVENT_SICK_RECOVERY, "stat": "health", "value": 41.0, "width": 1.0, "at": NOW})
    snapshot = state.snapshot_for_panel(now=NOW)
    assert "events" in snapshot
    block = snapshot["events"]
    assert block["total"] == 1
    assert len(block["history"]) == 1
    entry = block["history"][0]
    assert set(entry) <= {"key", "stat", "value", "width", "at"}
    assert entry["key"] == EVENT_SICK_RECOVERY


def test_tier_index_lookup_is_stable_for_every_stat() -> None:
    """事件层用到的档位反查覆盖全部五轴（防"新增一轴但忘了事件层"）。"""
    for stat in STAT_NAMES:
        assert tier_index_of(stat, 0.0) == 0
        assert tier_index_of(stat, 100.0) >= 0
