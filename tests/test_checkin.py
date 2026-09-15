"""签到与日历（v0.7.0）：纯函数、分片台账与入口行为的常驻门。

三组门各盯一条设计纪律（`core/checkin.py` 模块 docstring）：

1. **连续天数从集合现算**：补签必须"天然接链"——任何"补签后要不要重算计数器"的
   特判代码出现在这里都是红灯信号（本文件用接链行为测试把它钉死）。
2. **掷骰在后端、可注入**：幸运档用钉了种子的 `random.Random` 复现；
   区间反写/概率越界的坏配置不许抛异常。
3. **补签不给钱**：入口层把金币花掉但日志里绝不出现该日的收入记录。
"""

from __future__ import annotations

import random
import time
from typing import Any

from our_life.core.checkin import (
    CheckinOutcome,
    apply_luck,
    current_streak,
    days_between,
    is_valid_day,
    makeup_reason,
    next_checkin_streak,
    normalize_checkin_log,
    normalize_makeups,
    previous_day,
    reward_coins,
    roll_luck,
    week_key,
)
from our_life.core.configuration import CheckinSettings
from our_life.services.state import ShardState

CTX = {"_ctx": {"lanlan_name": "灵"}}


# ---------------------------------------------------------------------------
# 日期工具
# ---------------------------------------------------------------------------


def test_previous_day_crosses_month_and_year() -> None:
    assert previous_day("2026-09-01") == "2026-08-31"
    assert previous_day("2026-01-01") == "2025-12-31"
    assert previous_day("not-a-date") == ""


def test_days_between_sign_and_garbage() -> None:
    assert days_between("2026-09-10", "2026-09-14") == 4
    assert days_between("2026-09-14", "2026-09-10") == -4
    assert days_between("x", "2026-09-14") is None


def test_is_valid_day_rejects_lookalikes() -> None:
    assert is_valid_day("2026-09-14")
    assert not is_valid_day("2026-9-14")
    assert not is_valid_day("2026-02-30")  # 真实不存在的日期
    assert not is_valid_day("")
    assert not is_valid_day(None)
    assert not is_valid_day(20260914)


def test_week_key_is_iso_week() -> None:
    # 2026-09-14 是周一：同一个 ISO 周里取周中和周末验证同键，再验换周变键。
    assert week_key("2026-09-14") == week_key("2026-09-15")
    assert week_key("2026-09-13") != week_key("2026-09-14")
    assert week_key("garbage") == ""


# ---------------------------------------------------------------------------
# 台账归一化
# ---------------------------------------------------------------------------


def test_normalize_checkin_log_repairs_garbage() -> None:
    raw = [
        ["2026-09-02", 10, True],
        ["2026-09-01", "12", 1],  # 字符串数字、0/1 幸运
        ["2026-09-02", 99, False],  # 重复日期：后来者覆盖
        ["bad-day", 5, False],
        ["2026-09-03"],  # 缺列
        "junk",
        {"not": "a list"},
    ]
    log = normalize_checkin_log(raw)
    days = [entry[0] for entry in log]
    assert days == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert log[0][1:] == (12, True)  # 字符串数字收敛为 int，1 收敛为 True
    assert log[1][1:] == (99, False)
    assert log[2][1:] == (0, False)  # 缺列回退 (0, False)，但日期保留


def test_normalize_checkin_log_is_bounded_and_sorted() -> None:
    days = [f"2026-01-{index:02d}" for index in range(1, 29)]
    log = normalize_checkin_log([[day, 1, False] for day in reversed(days)])
    assert len(log) == 28
    assert [entry[0] for entry in log] == sorted(days)


def test_normalize_makeups_dedupes_and_validates() -> None:
    assert normalize_makeups(["2026-09-02", "2026-09-02", "nope", 5]) == ("2026-09-02",)


# ---------------------------------------------------------------------------
# 连续记录（纪律 1：读数从集合现算）
# ---------------------------------------------------------------------------


def _set(*days: str) -> frozenset[str]:
    return frozenset(days)


def test_current_streak_counts_suffix() -> None:
    checked = _set("2026-09-10", "2026-09-12", "2026-09-13", "2026-09-14")
    assert current_streak(checked, "2026-09-14") == 3  # 12/13/14 连续，10 断开
    assert current_streak(checked, "2026-09-15") == 3  # 今天没签 → 数到昨天
    assert current_streak(checked, "2026-09-11") == 1  # 今天没签，但昨天(10)签了 → 数到昨天：1 天
    assert current_streak(checked, "2026-09-16") == 0  # 今天与昨天都不在集合里 → 断链
    assert current_streak(_set(), "2026-09-14") == 0


def test_next_checkin_streak_with_and_without_chain() -> None:
    assert next_checkin_streak(_set("2026-09-13"), "2026-09-14") == 2
    assert next_checkin_streak(_set("2026-09-01"), "2026-09-14") == 1
    assert next_checkin_streak(_set(), "2026-09-14") == 1


def test_makeup_reconnects_streak_without_special_case() -> None:
    """接链的**行为证明**：12、13 签了，14 漏了，15 补签 14 后链条变成 4。

    这条测试是纪律 1 的钉子：若实现改回"增量计数器 + 补签特判"，这里必红。
    """
    state = ShardState(lanlan="灵")
    for day in ("2026-09-12", "2026-09-13"):
        state.note_checkin(day=day, coins=8, lucky=False, today=day)
    assert state.checkin_streak == 2
    # 14 号没签、15 号补 14：此刻"今天=15"，14 已补 → 后缀 12,13,14 连续
    state.note_makeup(day="2026-09-14", today="2026-09-15")
    assert state.checkin_streak == 3  # 截至昨天的链条长度
    # 15 号正常签到：昨天(14)在集合里 → 4
    state.note_checkin(day="2026-09-15", coins=14, lucky=False, today="2026-09-15")
    assert state.checkin_streak == 4
    assert state.checkin_best == 4


# ---------------------------------------------------------------------------
# 奖励曲线与幸运（纪律 2）
# ---------------------------------------------------------------------------


def test_reward_curve_and_cap() -> None:
    settings = CheckinSettings()
    coins = [
        reward_coins(
            streak=day,
            base_coins=settings.base_coins,
            streak_bonus_per_day=settings.streak_bonus_per_day,
            streak_cap_days=settings.streak_cap_days,
        )
        for day in range(1, 14)
    ]
    assert coins[0] == 8  # 第 1 天 = 基础值
    assert coins[3] == 14  # 8 + 2×3
    assert coins[10] == 28  # 封顶：8 + 2×10
    assert coins[11] == 28 and coins[12] == 28  # 第 12 天起不再增长


def test_roll_luck_seeded_and_bounded() -> None:
    rng = random.Random(20260914)
    factors = [roll_luck(rng, luck_chance=0.5, luck_min_bonus=0.5, luck_max_bonus=2.0) for _ in range(200)]
    hits = [factor for factor in factors if factor > 0.0]
    assert 40 < len(hits) < 160  # 概率量级正确（宽松界防种子脆断）
    assert all(0.5 <= factor <= 2.0 for factor in hits)
    # chance=0 永不中；chance=1 必中
    assert roll_luck(random.Random(1), luck_chance=0.0, luck_min_bonus=0.5, luck_max_bonus=2.0) == 0.0
    assert roll_luck(random.Random(1), luck_chance=1.5, luck_min_bonus=0.5, luck_max_bonus=2.0) > 0.0


def test_bad_luck_config_never_raises() -> None:
    """区间反写/概率越界：最多运气变差，不许抛（配置手改坏不炸入口）。"""
    settings = CheckinSettings.from_mapping({"luck_min_bonus": 3.0, "luck_max_bonus": 1.0})
    assert settings.luck_max_bonus >= settings.luck_min_bonus
    factor = roll_luck(
        random.Random(7),
        luck_chance=settings.luck_chance,
        luck_min_bonus=settings.luck_min_bonus,
        luck_max_bonus=settings.luck_max_bonus,
    )
    assert factor >= 0.0


def test_apply_luck_rounds_to_int() -> None:
    coins, lucky = apply_luck(10, 0.55)
    assert coins == 16 and lucky is True  # 10×1.55 = 15.5 → 四舍六入五成双 → 16
    coins, lucky = apply_luck(10, 0.0)
    assert coins == 10 and lucky is False


def test_checkin_outcome_shape() -> None:
    outcome = CheckinOutcome(day="2026-09-14", coins=9, lucky=True, streak=3, best=5)
    assert outcome.as_dict()["lucky"] is True


# ---------------------------------------------------------------------------
# 补签判定（顺序即优先级）
# ---------------------------------------------------------------------------


def test_makeup_reason_priority() -> None:
    checked = _set("2026-09-12", "2026-09-13")
    assert makeup_reason(day="nope", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "makeup_invalid_day"
    # 今天/未来：走正常签到，不许补
    assert makeup_reason(day="2026-09-14", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "makeup_invalid_day"
    assert makeup_reason(day="2026-09-20", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "makeup_invalid_day"
    # 已签过的日子不许再补（哪怕窗内）
    assert makeup_reason(day="2026-09-13", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "already_checked_in"
    # 窗口：9-06 距 9-14 正好 8 天 → 超出默认 7 天窗；9-07 是 7 天 → 窗内
    assert makeup_reason(day="2026-09-06", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "makeup_expired"
    assert makeup_reason(day="2026-09-07", today="2026-09-14", checked=checked, used_this_week=0, week_limit=1, window_days=7) == "ok"
    # 周额度
    assert makeup_reason(day="2026-09-11", today="2026-09-14", checked=checked, used_this_week=1, week_limit=1, window_days=7) == "makeup_exhausted"


# ---------------------------------------------------------------------------
# 分片序列化（schema 5 向后兼容）
# ---------------------------------------------------------------------------


def test_old_payload_gets_checkin_defaults() -> None:
    """v4 旧分片直接可读：签到台账缺键回退空账，不报错、不迁移。"""
    payload = {"schema_version": 4, "stats": {"mood": 60.0}, "sodas": 12}
    state = ShardState.from_payload("灵", payload, now=time.time())
    assert state.checkin_log == ()
    assert state.checkin_makeups == ()
    assert state.checkin_streak == 0 and state.checkin_best == 0
    assert state.makeup_used == 0


def test_checkin_roundtrip_keeps_lucky_flag() -> None:
    state = ShardState(lanlan="灵")
    state.note_checkin(day="2026-09-14", coins=13, lucky=True, today="2026-09-14")
    state.note_makeup(day="2026-09-10", today="2026-09-14")
    revived = ShardState.from_payload("灵", state.as_payload(), now=time.time())
    assert revived.checkin_log == (("2026-09-14", 13, True),)
    assert revived.checkin_makeups == ("2026-09-10",)
    assert revived.checkin_best == 1


def test_makeup_week_counter_resets_across_weeks() -> None:
    state = ShardState(lanlan="灵")
    state.begin_makeup_week(week=week_key("2026-09-14"))
    state.makeup_used = 1
    state.begin_makeup_week(week=week_key("2026-09-21"))
    assert state.makeup_used == 0


# ---------------------------------------------------------------------------
# 入口行为
# ---------------------------------------------------------------------------


def _enable(plugin: Any) -> None:
    from our_life.core.configuration import OurLifeSettings

    settings = OurLifeSettings.from_config({"our_life": {"enabled": True}})
    plugin._settings = settings





def test_checkin_flow_through_entry(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    _enable(plugin)
    today = time.strftime("%Y-%m-%d")

    first = run_async(plugin.checkin_entry(**CTX))
    assert first.is_ok()
    assert first.value["note"] == "checkin_done"
    assert first.value["coins"] >= 8
    assert first.value["streak"] == 1

    # 第二次必须被挡：一天一签
    second = run_async(plugin.checkin_entry(**CTX))
    assert not second.is_ok()
    assert str(second.error) == "already_checked_in"

    # 钱真的入账（幸运档最多 ×3：上限校验防配置漂移时入口失控）
    state = host_store_shard(host)
    assert 8 <= state["sodas"] <= 84
    log = state["checkin_log"]
    assert len(log) == 1 and log[0][0] == today


def host_store_shard(host: Any) -> dict[str, Any]:
    from our_life.services.state import shard_key

    return host.store.data[shard_key("灵")]


def test_checkin_respects_master_and_feature_switches(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    # 总开关关（fail-closed 默认）：冻结
    blocked = run_async(plugin.checkin_entry(**CTX))
    assert not blocked.is_ok()
    assert str(blocked.error) == "not_enabled"
    # 总开关开、子开关关：语义要分明，不能糊弄成 not_enabled
    from our_life.core.configuration import OurLifeSettings

    plugin._settings = OurLifeSettings.from_config(
        {"our_life": {"enabled": True, "checkin": {"enabled": False}}}
    )
    blocked = run_async(plugin.checkin_entry(**CTX))
    assert not blocked.is_ok()
    assert str(blocked.error) == "checkin_disabled"


def test_makeup_costs_coins_and_pays_nothing(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    _enable(plugin)
    state = ShardState(lanlan="灵", sodas=50, account_date="2026-01-01")
    # 直接布入内存缓存，让入口走真实判定链（StateStore.load 优先命中缓存）。
    plugin._store.cached["灵"] = state

    today = time.strftime("%Y-%m-%d")
    yesterday = previous_day(today)

    result = run_async(plugin.makeup_entry(day=yesterday, **CTX))
    assert result.is_ok(), result.error
    persisted = host_store_shard(host)
    assert persisted["sodas"] == 50 - plugin._settings.checkin.makeup_cost
    # 补签**不进**签到日志（只记 makeups，不给钱）
    assert persisted["checkin_log"] == []
    assert yesterday in persisted["checkin_makeups"]
    # 周额度立即生效
    second = run_async(plugin.makeup_entry(day=previous_day(yesterday), **CTX))
    assert not second.is_ok()
    assert str(second.error) == "makeup_exhausted"


def test_makeup_rejects_already_checked_day(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable(plugin)
    today = time.strftime("%Y-%m-%d")
    done = run_async(plugin.checkin_entry(**CTX))
    assert done.is_ok()
    result = run_async(plugin.makeup_entry(day=today, **CTX))
    assert not result.is_ok()
    assert str(result.error) == "makeup_invalid_day"  # 今天永远走正常签到


def test_dashboard_exposes_checkin_block(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable(plugin)
    context = run_async(plugin.dashboard_context(**CTX))
    block = context["checkin"]
    assert block["checked_today"] is False
    assert block["next_reward"] == plugin._settings.checkin.base_coins
    assert block["makeup_left"] == plugin._settings.checkin.makeup_week_limit
    assert block["enabled"] is True
    # 签完再看：预览归零、状态翻转
    assert run_async(plugin.checkin_entry(**CTX)).is_ok()
    after = run_async(plugin.dashboard_context(**CTX))["checkin"]
    assert after["checked_today"] is True
    assert after["next_reward"] == 0
