"""五轴卡明细门（v0.6.0）：`core.model.axis_details` + `_axis_view` 锚点语义。

面板上"距下一档还差 N 分"与档位徽章、注入判据**必须是同一条线**——所以 `to_next`
只许从 `TIER_BOUNDS` 现算（本门第 1 组）；"今日变化"的锚点是今日最早注入快照，
缺锚点必须是 `None` 而不是 0（本门第 2 组：拿 0 冒充"没变"是编数据）。
脏台账（坏 `at`）不许炸、也不许把锚点认错（第 3 组）。
"""

from __future__ import annotations

import time
from typing import Any

from our_life import _axis_view
from our_life.core.model import (
    STAT_NAMES,
    TIER_BOUNDS,
    Stats,
    axis_details,
    tier_index_of,
    tier_of,
)
from our_life.services.state import ShardState

# ---------------------------------------------------------------------------
# 1. 判据线 == 档位线
# ---------------------------------------------------------------------------


def _stats_with(**overrides: float) -> Stats:
    base = {"affection": 50.0, "mood": 50.0, "health": 50.0, "satiety": 50.0, "energy": 50.0}
    base.update(overrides)
    return Stats(**base)


def test_to_next_is_the_very_same_boundary_as_the_tier_table() -> None:
    """对每条分界线取两侧探针值：`to_next` 必须恰等于 `TIER_BOUNDS[i+1] - value`。

    谁改了 `TIER_BOUNDS` 而面板文案没跟上，这里先红——与 `core/events.py`
    "事件判据钉在档位表上"是同一条纪律的第 N 次应用。
    """
    for stat in STAT_NAMES:
        for probe in (0.0, 19.4, 20.0, 39.7, 60.0, 79.99, 80.0, 100.0):
            detail = axis_details(_stats_with(**{stat: probe}))[stat]
            index = tier_index_of(stat, probe)
            assert detail["tier"] == tier_of(stat, probe)
            assert detail["tier_index"] == index
            if index + 1 >= len(TIER_BOUNDS):
                assert detail["next_tier"] is None and detail["to_next"] is None
            else:
                assert detail["to_next"] == round(TIER_BOUNDS[index + 1] - probe, 1)
                assert detail["to_next"] >= 0


def test_top_tier_has_no_next_and_lowest_reports_index_zero() -> None:
    detail = axis_details(_stats_with(mood=100.0))["mood"]
    assert detail["next_tier"] is None and detail["to_next"] is None
    bottom = axis_details(_stats_with(mood=0.0))["mood"]
    assert bottom["tier_index"] == 0
    assert bottom["to_next"] == TIER_BOUNDS[1]


def test_keys_cover_every_axis_and_values_survive_dirty_clamping() -> None:
    details = axis_details(_stats_with(energy=140.0, satiety=float("nan")))
    assert set(details) == set(STAT_NAMES)
    assert details["energy"]["value"] == 100.0
    assert details["satiety"]["value"] == 0.0  # NaN → 下限，不炸


# ---------------------------------------------------------------------------
# 2. 今日变化的锚点语义
# ---------------------------------------------------------------------------


def test_delta_is_absent_without_an_anchor_never_zero() -> None:
    detail = axis_details(_stats_with(mood=50.0))["mood"]
    assert detail["delta_today"] is None


def test_delta_measures_against_the_given_day_start() -> None:
    details = axis_details(_stats_with(mood=50.0), day_start=_stats_with(mood=41.2))
    assert details["mood"]["delta_today"] == 8.8
    down = axis_details(_stats_with(mood=30.0), day_start=_stats_with(mood=32.0))
    assert down["mood"]["delta_today"] == -2.0


# ---------------------------------------------------------------------------
# 3. `_axis_view`：从注入台账里挑今日锚点
# ---------------------------------------------------------------------------


def _entry(at: float, **stats: float) -> dict[str, Any]:
    base = {name: 50.0 for name in STAT_NAMES}
    base.update(stats)
    return {"at": at, "trigger": "interval", "summary": "", "stats": base}


def test_axis_view_uses_oldest_today_snapshot_as_anchor() -> None:
    """锈点 = 今日**最早**那条；昨日的更早快照（mood=10）必须被跳过。

    时刻用"刚刚"而不是"今晨两小时前"：凌晨零点左右跑测试时，固定往前推几小时
    会一脚踏进昨天——那会让本门在真半夜变成偶发红，而偶发红的门等于没有门。
    """
    now = time.time()
    state = ShardState(
        lanlan="灵",
        stats=_stats_with(mood=60.0),
        inject_history=(
            _entry(now - 26 * 3600.0, mood=10.0),  # 昨天：更早但不许当锚点
            _entry(now, mood=55.0),  # 今日内最早 → 锚点
            _entry(now, mood=58.0),  # 也是今日，但不是最早
        ),
    )
    details = _axis_view(state, now=now)
    assert details["mood"]["delta_today"] == 5.0


def test_axis_view_returns_none_delta_when_today_is_blank() -> None:
    now = time.time()
    yesterday = now - 26 * 3600.0
    state = ShardState(
        lanlan="灵",
        stats=_stats_with(mood=60.0),
        inject_history=(_entry(yesterday, mood=40.0),),
    )
    details = _axis_view(state, now=now)
    assert details["mood"]["delta_today"] is None


def test_axis_view_skips_dirty_entries_without_exploding() -> None:
    """脏台账不许炸、也不许挡在今日锚点前面吞掉真锚点。"""
    now = time.time()
    state = ShardState(
        lanlan="灵",
        stats=_stats_with(mood=60.0),
        inject_history=(
            {"at": "not-a-number", "stats": {"mood": 1.0}},  # 坏时刻 → 跳过
            {"at": now, "summary": "no stats"},  # 没快照 → 跳过
            _entry(now, mood=45.0),  # 真锚点
        ),
    )
    details = _axis_view(state, now=now)
    assert details["mood"]["delta_today"] == 15.0

# ---------------------------------------------------------------------------
# 4. context 接线：axes 与档位徽章同源
# ---------------------------------------------------------------------------


def _shard_payload(*, mood: float, now: float) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "stats": {name: 50.0 for name in STAT_NAMES} | {"mood": mood},
        "last_decay_at": now,
        "last_touch_at": now - 60.0,
        "streak_days": 3,
        "last_active_date": "",
        "milestones": [],
        "neglect_days_applied": 0.0,
        "session_key": "",
        "session_turns": 0,
        "last_inject_at": None,
        "inject_timestamps": [],
        "inject_history": [],
        "company_last_at": None,
        "seen_conversation_ids": [],
        "hour_histogram": [0] * 24,
        "updated_at": now,
    }


def test_dashboard_axes_match_snapshot_tiers(make_plugin: Any, run_async: Any) -> None:
    """面板上五轴卡的档名与状态带徽章同源：axes[key].tier == state.tiers[key]，
    且 axes 覆盖全部五轴、旧字段一个不少（新增只增不改）。"""
    plugin, host = make_plugin()
    now = time.time()
    host.store.data["ourlife@灵"] = _shard_payload(mood=61.0, now=now)
    payload = run_async(plugin.dashboard_context(_ctx={"lanlan_name": "灵"}))
    axes = payload["axes"]
    assert set(axes) == set(STAT_NAMES)
    for key, detail in axes.items():
        assert detail["tier"] == payload["state"]["tiers"][key]
    # 今天没注入过 → 各轴 delta 都是 None（缺锚点不假扮 0）。
    assert all(detail["delta_today"] is None for detail in axes.values())


def test_dashboard_axes_absent_without_shard(make_plugin: Any, run_async: Any) -> None:
    """无分片路径不应硬造一份 axes；面板对缺字段容错（呈现层降级，不炸）。"""
    plugin, _host = make_plugin()
    payload = run_async(plugin.dashboard_context())
    assert "axes" not in payload
