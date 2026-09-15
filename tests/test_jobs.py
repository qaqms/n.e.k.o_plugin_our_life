"""猫娘打工（v0.7.0）：班次判据、结算数学与入口行为的常驻门。

盯四条纪律（`core/jobs.py` 模块 docstring）：

1. **工钱系数只用精力/心情/健康**——饱食低会被"该吃饭"自然惩罚，再进工资是双重扣；
   好感是长期量，不该出现在时薪里。
2. **早退与到点共用一个 `settle_shift`**：早退只是把同一个函数提前调了
   （fraction < 1 → 自动折价、按比例扣损耗），实现里不该长出第二套算法。
3. **睡眠窗判据是"整段班次"**：哪怕最后一小时才压到睡眠窗也要挡。
4. **坏班次必须能自愈**：未知 job id / 时间字段损坏时清班并继续工作，
   鬼班次会永久挡住下一次开工（`already_working`），比丢一笔钱更糟。
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from our_life.core.configuration import JobSettings, OurLifeSettings
from our_life.core.jobs import (
    MAX_SHIFTS_PER_DAY,
    WAGE_FACTOR_CEIL,
    WAGE_FACTOR_FLOOR,
    apply_shift_costs,
    job_catalog,
    job_narration,
    settle_shift,
    shift_hits_sleep_window,
    start_block_reason,
    wage_factor,
    wage_preview,
)
from our_life.core.model import Stats
from our_life.services.state import ShardState

CTX = {"_ctx": {"lanlan_name": "灵"}}


def _enable_jobs(plugin: Any, *, always_awake: bool = True, **job_overrides: Any) -> None:
    """开总开关 + 打工开关；默认把睡眠窗钉成"永远清醒"。

    不这么做的话，判据门会在夜里跑时变红（真实钟点落进默认睡眠窗 24→8，
    `job_sleep_window` 把开工挡掉）——测试时间相关 = 必脆。"睡觉被挡"的行为
    由 `test_job_start_blocks_sleeping` 用注入的 sleeping 判据专门验。
    """
    job_table: dict[str, Any] = {"enabled": True}
    job_table.update(job_overrides)
    config: dict[str, Any] = {"our_life": {"enabled": True, "job": job_table}}
    if always_awake:
        config["our_life"]["rhythm"] = {"sleep_start_hour": 8, "sleep_end_hour": 8}
    plugin._settings = OurLifeSettings.from_config(config)


# ---------------------------------------------------------------------------
# 工钱
# ---------------------------------------------------------------------------


def test_wage_factor_endpoints_and_bounds() -> None:
    assert abs(wage_factor(Stats(energy=0.0, mood=0.0, health=0.0)) - WAGE_FACTOR_FLOOR) < 1e-9
    assert abs(wage_factor(Stats(energy=100.0, mood=100.0, health=100.0)) - WAGE_FACTOR_CEIL) < 1e-9
    rng = random.Random(4)
    for _ in range(100):
        stats = Stats(
            energy=rng.uniform(0, 100),
            mood=rng.uniform(0, 100),
            health=rng.uniform(0, 100),
            satiety=rng.uniform(0, 100),  # 故意撒随机值：纪律 1 要求系数**不看**它
            affection=rng.uniform(0, 100),
        )
        assert WAGE_FACTOR_FLOOR - 1e-9 <= wage_factor(stats) <= WAGE_FACTOR_CEIL + 1e-9


def test_wage_factor_ignores_satiety_and_affection() -> None:
    left = Stats(energy=60, mood=60, health=60, satiety=5, affection=100)
    right = Stats(energy=60, mood=60, health=60, satiety=95, affection=0)
    assert abs(wage_factor(left) - wage_factor(right)) < 1e-9


def test_wage_preview_span() -> None:
    low, high = wage_preview("konbini") or (0, 0)
    assert low == round(25 * WAGE_FACTOR_FLOOR)
    assert high == round(25 * WAGE_FACTOR_CEIL)
    assert wage_preview("nope") is None


# ---------------------------------------------------------------------------
# 睡眠窗
# ---------------------------------------------------------------------------


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 14, hour, minute, 0)


def test_shift_hits_sleep_window_default_night() -> None:
    # 默认睡眠窗 24:00→08:00
    assert not shift_hits_sleep_window(
        start=_dt(10), hours=2, sleep_start_hour=24, sleep_end_hour=8
    )
    assert shift_hits_sleep_window(
        start=_dt(23), hours=2, sleep_start_hour=24, sleep_end_hour=8
    )  # 23→1 压进睡眠窗
    assert shift_hits_sleep_window(
        start=_dt(6), hours=3, sleep_start_hour=24, sleep_end_hour=8
    )  # 清晨还没醒
    assert not shift_hits_sleep_window(
        start=_dt(8), hours=8, sleep_start_hour=24, sleep_end_hour=8
    )  # 起床后到 16:00 全程清醒


def test_shift_hits_sleep_window_degenerate_and_zero() -> None:
    # 起止相同 = 永远清醒（与 rhythm 的退化配置同一语义）
    assert not shift_hits_sleep_window(
        start=_dt(3), hours=2, sleep_start_hour=8, sleep_end_hour=8
    )
    assert not shift_hits_sleep_window(start=_dt(3), hours=0.0, sleep_start_hour=24, sleep_end_hour=8)


# ---------------------------------------------------------------------------
# 开工判据（顺序即优先级）
# ---------------------------------------------------------------------------


def _block(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "job_id": "konbini",
        "stats": Stats(energy=80, satiety=80, mood=60, health=70),
        "active_job_id": "",
        "shifts_today": 0,
        "max_per_day": 2,
        "sleeping": False,
        "hits_sleep_window": False,
    }
    args.update(overrides)
    return start_block_reason(**args)


def test_start_block_reason_priority() -> None:
    assert _block() == "ok"
    assert _block(job_id="nope") == "invalid_job"
    assert _block(active_job_id="konbini") == "already_working"
    assert _block(sleeping=True) == "job_sleep_window"
    assert _block(hits_sleep_window=True) == "job_sleep_window"
    assert _block(shifts_today=2, max_per_day=2) == "job_daily_limit"
    # 门槛：night_market 要 energy>=50 且 satiety>=40
    assert _block(job_id="night_market", stats=Stats(energy=49, satiety=90)) == "job_needs_rest"
    assert _block(job_id="night_market", stats=Stats(energy=60, satiety=39)) == "job_needs_rest"
    assert _block(job_id="night_market", stats=Stats(energy=60, satiety=40)) == "ok"
    # 无门槛的工作不看身体
    assert _block(stats=Stats(energy=0, satiety=0)) == "ok"


# ---------------------------------------------------------------------------
# 结算数学
# ---------------------------------------------------------------------------


def test_settle_shift_on_time_full_pay_with_state_factor() -> None:
    started, ends = 1000.0, 1000.0 + 2 * 3600.0
    outcome = settle_shift(
        job_id="konbini",
        now=ends + 60.0,  # 到点后哪怕隔很久才结，fraction 也封顶 1
        started_at=started,
        end_at=ends,
        stats=Stats(energy=100, mood=100, health=100),
        early_leave_ratio=0.6,
    )
    assert outcome is not None
    assert outcome.fraction == 1.0 and outcome.early is False
    assert outcome.pay == round(25 * WAGE_FACTOR_CEIL)
    assert dict(outcome.costs) == {"energy": 10.0, "satiety": 8.0, "mood": 4.0}


def test_settle_shift_early_prorates_pay_and_costs() -> None:
    started, ends = 0.0, 4 * 3600.0
    outcome = settle_shift(
        job_id="night_market",
        now=2 * 3600.0,  # 干满一半
        started_at=started,
        end_at=ends,
        stats=Stats(energy=100, mood=100, health=100),
        early_leave_ratio=0.5,
    )
    assert outcome is not None and outcome.early is True
    # 60 × 1.1(系数) × 0.5(时长) × 0.5(早退折) = 16.5 → 16（round half to even）
    assert outcome.pay in (16, 17)
    assert dict(outcome.costs)["energy"] == 11.0  # 22 的一半
    assert outcome.as_dict()["job"] == "night_market"


def test_settle_shift_zero_and_garbage_inputs() -> None:
    assert settle_shift(job_id="nope", now=1.0, started_at=0.0, end_at=2.0, stats=Stats(), early_leave_ratio=0.6) is None
    outcome = settle_shift(
        job_id="konbini", now=0.0, started_at=10.0, end_at=20.0, stats=Stats(), early_leave_ratio=0.6
    )  # 在开始之前就被结（时间被手改坏）：fraction=0 → 0 元、0 损耗
    assert outcome is not None and outcome.pay == 0
    assert all(delta == 0.0 for _name, delta in outcome.costs)


def test_apply_shift_costs_clamps_at_floor() -> None:
    stats = Stats(energy=5, satiety=3, mood=1)
    outcome = settle_shift(
        job_id="night_market",
        now=1000.0 + 4 * 3600.0,
        started_at=1000.0,
        end_at=1000.0 + 4 * 3600.0,
        stats=stats,
        early_leave_ratio=0.6,
    )
    assert outcome is not None
    applied = apply_shift_costs(stats, outcome)
    assert applied.energy == 0.0 and applied.satiety == 0.0 and applied.mood == 0.0
    assert applied.health == stats.health  # 不碰没在损耗表里的轴


# ---------------------------------------------------------------------------
# 叙事与目录
# ---------------------------------------------------------------------------


def test_job_narration_shape() -> None:
    line = job_narration("konbini", pay=27, early=False)
    assert "27" in line and "叫回来" not in line
    assert job_narration("nope", pay=1, early=False) == ""
    assert "叫回来" in job_narration("mascot", pay=40, early=True)


def test_job_catalog_is_complete_and_closes_high() -> None:
    catalog = job_catalog()
    ids = {entry["id"] for entry in catalog}
    assert ids == {"konbini", "mascot", "night_market"}
    for entry in catalog:
        assert entry["pay_high"] >= entry["pay_low"] > 0
        assert entry["costs"]


# ---------------------------------------------------------------------------
# 分片台账
# ---------------------------------------------------------------------------


def test_shift_begin_and_end_roundtrip() -> None:
    state = ShardState(lanlan="灵")
    state.begin_shift(job_id="konbini", now=100.0, hours=2.0)
    assert state.working and state.job_end_at == 100.0 + 7200.0
    assert state.shift_remaining(now=100.0 + 3600.0) == 3600.0
    revived = ShardState.from_payload("灵", state.as_payload(), now=time.time())
    assert revived.working and revived.job_id == "konbini"
    revived.end_shift()
    assert not revived.working and revived.shift_remaining(now=time.time()) == 0.0


def test_job_day_counter_resets_across_days() -> None:
    state = ShardState(lanlan="灵", job_day="2026-09-13", job_count_today=2)
    assert state.reset_job_day(today="2026-09-13") is False
    assert state.job_count_today == 2
    assert state.reset_job_day(today="2026-09-14") is True
    assert state.job_count_today == 0


def test_old_payload_has_no_job_fields_but_loads() -> None:
    revived = ShardState.from_payload("灵", {"schema_version": 4, "stats": {}}, now=time.time())
    assert revived.job_id == "" and not revived.working


# ---------------------------------------------------------------------------
# 入口行为
# ---------------------------------------------------------------------------


def test_job_start_sets_persisted_shift(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    _enable_jobs(plugin)
    result = run_async(plugin.job_start_entry(job="konbini", **CTX))
    assert result.is_ok(), result.error
    assert result.value["note"] == "job_started"
    assert result.value["ends_at"] > time.time()
    payload = host.store.data["ourlife@灵"]
    assert payload["job_id"] == "konbini" and payload["job_count_today"] == 1
    # 第二份工被挡
    second = run_async(plugin.job_start_entry(job="mascot", **CTX))
    assert not second.is_ok()
    assert str(second.error) == "already_working"


def test_job_start_daily_limit_and_invalid(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    _enable_jobs(plugin, max_shifts_per_day=1)
    assert run_async(plugin.job_start_entry(job="konbini", **CTX)).is_ok()
    state = plugin._store.cached["灵"]
    state.end_shift()  # 模拟"下班了"，再开下一班
    blocked = run_async(plugin.job_start_entry(job="konbini", **CTX))
    assert not blocked.is_ok()
    assert str(blocked.error) == "job_daily_limit"
    bogus = run_async(plugin.job_start_entry(job="moonlighting", **CTX))
    assert not bogus.is_ok()
    assert str(bogus.error) == "invalid_job"


def test_job_start_blocks_sleeping(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    real_rhythm = plugin._rhythm
    plugin._rhythm = lambda **kwargs: type(
        "R", (), {"sleeping": True, "date_iso": "2026-09-14", "phase": "night", "awake_ratio": 0.0}
    )()
    try:
        blocked = run_async(plugin.job_start_entry(job="konbini", **CTX))
    finally:
        plugin._rhythm = real_rhythm
    assert not blocked.is_ok()
    assert str(blocked.error) == "job_sleep_window"


def test_job_start_requires_stats(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    state = ShardState(lanlan="灵", stats=Stats(energy=20, satiety=20))
    plugin._store.cached["灵"] = state
    blocked = run_async(plugin.job_start_entry(job="night_market", **CTX))
    assert not blocked.is_ok()
    assert str(blocked.error) == "job_needs_rest"


def test_job_return_prorates_early_shift(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable_jobs(plugin, early_leave_ratio=0.5)
    started = time.time() - 3600.0  # 一小时前上的班
    state = ShardState(lanlan="灵", stats=Stats(energy=100, mood=100, health=100))
    state.begin_shift(job_id="night_market", now=started, hours=4.0)
    plugin._store.cached["灵"] = state
    result = run_async(plugin.job_return_entry(**CTX))
    assert result.is_ok(), result.error
    assert result.value["note"] == "job_returned"
    assert abs(result.value["fraction"] - 0.25) < 0.01
    # 60 × 1.1 × 0.25 × 0.5 ≈ 8.25 → 8
    assert result.value["pay"] in (8, 9)
    assert result.value["early"] is True
    assert plugin._store.cached["灵"].job_id == ""


def test_job_return_without_shift_is_honest(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    state = ShardState(lanlan="灵")
    plugin._store.cached["灵"] = state
    result = run_async(plugin.job_return_entry(**CTX))
    assert not result.is_ok()
    assert str(result.error) == "not_working"


def test_tick_settles_shift_on_time(make_plugin: Any, run_async: Any) -> None:
    """到点结算走的是 tick 的时间判据——没有任何常驻计时器参与。"""
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    started = time.time() - 3 * 3600.0
    state = ShardState(lanlan="灵", stats=Stats(energy=100, mood=100, health=100))
    state.begin_shift(job_id="konbini", now=started, hours=2.0)
    plugin._store.cached["灵"] = state
    before_sodas = state.sodas
    settled = plugin._settle_job(state, stats=state.stats, now=time.time())
    assert settled is not None
    outcome, new_stats = settled
    assert outcome.fraction == 1.0 and outcome.pay == round(25 * WAGE_FACTOR_CEIL)
    assert state.sodas == before_sodas + outcome.pay
    assert state.job_id == "" and state.job_shifts_total == 1
    assert new_stats.energy < 100.0  # 额外损耗真的扣了


def test_malformed_shift_self_heals(make_plugin: Any, run_async: Any) -> None:
    """纪律 4：未知 job id 的鬼班次必须被清掉，而不是永远挡着开工。"""
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    state = ShardState(lanlan="灵", job_id="ghost_gig", job_start_at=1.0, job_end_at=2.0)
    plugin._store.cached["灵"] = state
    assert plugin._settle_job(state, stats=Stats(), now=time.time()) is None
    assert state.job_id == ""
    again = run_async(plugin.job_start_entry(job="konbini", **CTX))
    assert again.is_ok()


def test_job_gates_follow_switches(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    # 总开关关
    blocked = run_async(plugin.job_start_entry(job="konbini", **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "not_enabled"
    # 总开关开、打工子开关关
    _enable_jobs(plugin, enabled=False)
    blocked = run_async(plugin.job_start_entry(job="konbini", **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "jobs_disabled"


def test_dashboard_exposes_job_board(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    _enable_jobs(plugin)
    context = run_async(plugin.dashboard_context(**CTX))
    job_view = context["job"]
    assert job_view["enabled"] is True
    assert job_view["id"] == ""
    assert len(job_view["catalog"]) == 3
    assert run_async(plugin.job_start_entry(job="konbini", **CTX)).is_ok()
    after = run_async(plugin.dashboard_context(**CTX))["job"]
    assert after["id"] == "konbini"
    assert after["remaining_sec"] > 6000.0
    assert after["today_count"] == 1


def test_job_settings_clamped() -> None:
    # 代码天花板：配置把日上限调到 99 也只认 MAX_SHIFTS_PER_DAY
    assert JobSettings.from_mapping({"max_shifts_per_day": 99}).max_shifts_per_day == MAX_SHIFTS_PER_DAY
    assert JobSettings.from_mapping({"max_shifts_per_day": -3}).max_shifts_per_day == 0
    assert JobSettings.from_mapping({"early_leave_ratio": 5.0}).early_leave_ratio == 1.0
    assert JobSettings.from_mapping({"early_leave_ratio": "x"}).early_leave_ratio == 0.6
