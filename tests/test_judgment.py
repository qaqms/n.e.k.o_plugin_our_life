"""反馈闭环门（v0.3.0）：`core/judgment.judge` 的四条闸门 + 入口/tick 接线。

这一轮的风险面比之前任何一块都大，因为**数值变化第一次来自模型的自我描述**。
所以门是分层写的，而且每条闸门都有独立的正面/反面用例：

1. **单次幅度**：她报一次开心必须**严格小于**主人多说一句话给的量。
2. **会话递减**：同一会话里第 n 次判断的影响按 `1/(1+n*damp)` 变小。
3. **每日上限**：正向与负向**分开**，用完了就不再修正（且负向天花板只有正向一半）。
4. **真实互动作前提**：`last_touch_at > last_judgment_at`——没有它，模型能在没有任何
   用户发言的回合里反复调用工具把自己刷满。**这条是整个功能的安全底线。**

另有一组"不可信输入"门：非法标签、越界强度、非字符串、缺参数一律收敛成无害结果，
且工具**永不抛异常**。

接线上还钉两条：
- 工具只负责**记账 + 排队**，数值改动由 tick 统一施加（保证走在衰减/耦合同一条链上）；
- 非法标签收敛后**不算错误**（`ok=False` 但 `reason=judgment_silent`），
  不把噪声引回对话。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from our_life.core.configuration import FeedbackSettings, GrowthSettings
from our_life.core.injection import TRIGGER_JUDGMENT
from our_life.core.judgment import (
    JUDGMENT_LABELS,
    JUDGMENT_WEIGHTS,
    LABEL_DULL,
    LABEL_GOOD,
    LABEL_HURT,
    LABEL_NEUTRAL,
    LABEL_WONDERFUL,
    REASON_DAILY_CAP,
    REASON_DISABLED,
    REASON_NO_INTERACTION,
    REASON_OK,
    REASON_SILENT,
    judge,
    judged_axes,
    normalize_label,
    normalize_strength,
)
from our_life.core.model import STAT_NAMES, Stats, apply_judgment

SHARD_KEY = "ourlife@灵"
_GROWTH = GrowthSettings()
_FEEDBACK = FeedbackSettings()


def _judge(
    *,
    label: Any,
    strength: Any = None,
    feedback: FeedbackSettings | None = None,
    growth: GrowthSettings | None = None,
    enabled: bool = True,
    last_touch_at: float | None = 1000.0,
    last_judgment_at: float | None = None,
    session_turns: int = 0,
    day_used_add: float = 0.0,
    day_used_subtract: float = 0.0,
) -> Any:
    """带默认值的薄包装，让每条门只写它真正关心的那个参数。"""
    return judge(
        label=label,
        strength=strength,
        feedback=feedback or _FEEDBACK,
        growth=growth or _GROWTH,
        enabled=enabled,
        last_touch_at=last_touch_at,
        last_judgment_at=last_judgment_at,
        session_turns=session_turns,
        day_used_add=day_used_add,
        day_used_subtract=day_used_subtract,
    )


# ---------------------------------------------------------------------------
# 不可信输入：白名单与收敛
# ---------------------------------------------------------------------------


def test_labels_are_the_five_documented_ones() -> None:
    assert JUDGMENT_LABELS == (LABEL_WONDERFUL, LABEL_GOOD, LABEL_NEUTRAL, LABEL_DULL, LABEL_HURT)
    assert set(JUDGMENT_WEIGHTS) == set(JUDGMENT_LABELS)


def test_unknown_labels_collapse_to_neutral() -> None:
    for value in ("ecstatic", "", "  ", "WONDERFULL", 123, None, ["wonderful"], {"label": "good"}):
        assert normalize_label(value) == LABEL_NEUTRAL, f"{value!r} should collapse to neutral"


def test_label_parsing_is_case_and_whitespace_insensitive() -> None:
    assert normalize_label("  Wonderful  ") == LABEL_WONDERFUL
    assert normalize_label("HURT") == LABEL_HURT
    assert normalize_label("Good") == LABEL_GOOD


def test_strength_is_clamped_and_never_poisons_the_result() -> None:
    assert normalize_strength(None) == 1.0
    assert normalize_strength("") == 1.0
    assert normalize_strength("abc") == 1.0
    assert normalize_strength(True) == 1.0
    assert normalize_strength(float("nan")) == 1.0
    assert normalize_strength(0.0) == 1.0
    assert normalize_strength(-3.0) == 1.0
    assert normalize_strength(5.0) == 1.0  # 夹到 1
    assert normalize_strength(0.25) == 0.25


def test_invalid_label_is_a_silent_no_op_not_an_error() -> None:
    """非法标签必须**什么都不改**：报错会把噪声引回模型，改数值则是最糟的解读。"""
    result = _judge(label="ecstatic")
    assert result.label == LABEL_NEUTRAL
    assert result.reason == REASON_SILENT
    assert not result.applied
    assert result.mood == 0.0 and result.health == 0.0 and result.affection == 0.0


def test_neutral_label_changes_nothing() -> None:
    result = _judge(label=LABEL_NEUTRAL)
    assert result.reason == REASON_SILENT
    assert not result.applied


# ---------------------------------------------------------------------------
# 闸门 ①：单次幅度 < 一次真实互动
# ---------------------------------------------------------------------------


def test_gate1_single_wonderful_is_smaller_than_one_real_turn() -> None:
    result = _judge(label=LABEL_WONDERFUL)
    assert result.reason == REASON_OK
    assert result.applied
    assert 0.0 < result.mood < _GROWTH.turn_mood_gain
    assert 0.0 < result.health < _GROWTH.turn_health_gain
    assert 0.0 < result.affection < _GROWTH.turn_affection_gain


def test_gate1_amplitude_tracks_the_growth_constants() -> None:
    """把 `turn_mood_gain` 调大，单次判断幅度必须跟着变——否则就是写死的数字在漂移。"""
    doubled = GrowthSettings(turn_mood_gain=_GROWTH.turn_mood_gain * 2.0)
    base = _judge(label=LABEL_WONDERFUL)
    bumped = _judge(label=LABEL_WONDERFUL, growth=doubled)
    assert bumped.mood == pytest.approx(base.mood * 2.0)
    assert bumped.mood < doubled.turn_mood_gain


def test_gate1_never_touches_satiety_or_energy() -> None:
    """反馈只能改"感受"三轴：饱食/精力归作息与进食，让判断去改会和 apply_meal 打架。"""
    assert set(judged_axes()) == {"mood", "health", "affection"}
    result = _judge(label=LABEL_WONDERFUL)
    assert set(result.as_deltas()) == {"mood", "health", "affection"}
    assert "satiety" not in result.as_deltas() and "energy" not in result.as_deltas()


def test_gate1_strength_scales_the_amplitude() -> None:
    full = _judge(label=LABEL_GOOD, strength=1.0)
    half = _judge(label=LABEL_GOOD, strength=0.5)
    assert half.mood == pytest.approx(full.mood * 0.5)


# ---------------------------------------------------------------------------
# 闸门 ②：会话内递减
# ---------------------------------------------------------------------------


def test_gate2_session_damping_shrinks_later_judgments() -> None:
    first = _judge(label=LABEL_WONDERFUL, session_turns=0)
    second = _judge(label=LABEL_WONDERFUL, session_turns=1)
    third = _judge(label=LABEL_WONDERFUL, session_turns=2)
    assert first.mood > second.mood > third.mood
    # damp=0.5 → 1/(1+0.5*1) = 2/3，1/(1+0.5*2) = 1/2
    assert second.mood == pytest.approx(first.mood * (2.0 / 3.0))
    assert third.mood == pytest.approx(first.mood * 0.5)


def test_gate2_a_new_session_restores_the_full_step() -> None:
    """跨会话 `session_turns` 归零（既有字段的既有语义），判断也随之回到满步长。"""
    assert _judge(label=LABEL_WONDERFUL, session_turns=5).mood < _judge(
        label=LABEL_WONDERFUL, session_turns=0
    ).mood


def test_gate2_zero_damp_disables_the_diminishing() -> None:
    flat = FeedbackSettings(session_damp=0.0)
    first = _judge(label=LABEL_WONDERFUL, feedback=flat, session_turns=0)
    later = _judge(label=LABEL_WONDERFUL, feedback=flat, session_turns=7)
    assert later.mood == pytest.approx(first.mood)


# ---------------------------------------------------------------------------
# 闸门 ③：每日上限（正向 / 负向分开）
# ---------------------------------------------------------------------------


def test_gate3_positive_budget_exhausts_and_then_refuses() -> None:
    feedback = FeedbackSettings(daily_add_points=2.0)
    first = _judge(label=LABEL_WONDERFUL, feedback=feedback, day_used_add=0.0)
    assert first.reason == REASON_OK
    assert first.mood <= 2.0

    spent = _judge(label=LABEL_WONDERFUL, feedback=feedback, day_used_add=2.0)
    assert spent.reason == REASON_DAILY_CAP
    assert not spent.applied


def test_gate3_partial_budget_clips_instead_of_being_wasted() -> None:
    """剩一点点额度时应当**裁到剩余量**，而不是整笔丢掉。"""
    feedback = FeedbackSettings(daily_add_points=10.0)
    clipped = _judge(label=LABEL_WONDERFUL, feedback=feedback, day_used_add=9.5)
    assert clipped.reason == REASON_OK
    assert clipped.mood == pytest.approx(0.5, abs=1e-9)


def test_gate3_budgets_are_direction_separate() -> None:
    """正向额度用光不该影响负向：两个预算是两本账。"""
    feedback = FeedbackSettings(daily_add_points=1.0, daily_subtract_points=1.0)
    assert _judge(label=LABEL_WONDERFUL, feedback=feedback, day_used_add=1.0).reason == REASON_DAILY_CAP
    negative = _judge(label=LABEL_HURT, feedback=feedback, day_used_add=1.0, day_used_subtract=0.0)
    assert negative.reason == REASON_OK
    assert negative.mood < 0.0


def test_gate3_negative_direction_of_the_default_budget_is_the_smaller_half() -> None:
    assert _FEEDBACK.daily_subtract_points < _FEEDBACK.daily_add_points
    hurt = _judge(label=LABEL_HURT)
    wonderful = _judge(label=LABEL_WONDERFUL)
    assert abs(hurt.mood) == pytest.approx(wonderful.mood)  # 权重对称，天花板不对称


def test_gate3_zero_budget_turns_the_feature_off_gracefully() -> None:
    feedback = FeedbackSettings(daily_add_points=0.0, daily_subtract_points=0.0)
    assert _judge(label=LABEL_WONDERFUL, feedback=feedback).reason == REASON_DAILY_CAP
    assert _judge(label=LABEL_HURT, feedback=feedback).reason == REASON_DAILY_CAP


# ---------------------------------------------------------------------------
# 闸门 ④：真实互动作前提（安全底线）
# ---------------------------------------------------------------------------


def test_gate4_never_touched_means_no_correction() -> None:
    result = _judge(label=LABEL_WONDERFUL, last_touch_at=None)
    assert result.reason == REASON_NO_INTERACTION
    assert not result.applied


def test_gate4_a_judgment_must_be_paid_for_by_new_interaction() -> None:
    """同一次互动的"消费权"只能被用掉一次：上一次判断之后必须还有新的 `last_touch`。

    没有这条，模型可以在没有任何用户发言的回合里反复调用工具，把额度一点点刷满。
    这是整个反馈闭环**最不能妥协**的一条。
    """
    # 同一个时刻：这次互动已经被上一次判断消费掉了
    consumed = _judge(label=LABEL_WONDERFUL, last_touch_at=1000.0, last_judgment_at=1000.0)
    assert consumed.reason == REASON_NO_INTERACTION
    assert not consumed.applied

    # 更早的互动更不行（时间倒流说明没有新互动）
    assert (
        _judge(label=LABEL_WONDERFUL, last_touch_at=900.0, last_judgment_at=1000.0).reason
        == REASON_NO_INTERACTION
    )

    # 有了新互动才放行
    fresh = _judge(label=LABEL_WONDERFUL, last_touch_at=1001.0, last_judgment_at=1000.0)
    assert fresh.reason == REASON_OK
    assert fresh.applied


def test_gate4_gate_order_reports_the_most_specific_reason() -> None:
    """开关关着时不该报"缺互动"——原因码要指向真正拦住它的那道闸门。"""
    assert _judge(label=LABEL_WONDERFUL, enabled=False, last_touch_at=None).reason == REASON_DISABLED
    assert _judge(label=LABEL_WONDERFUL, last_touch_at=None).reason == REASON_NO_INTERACTION
    assert _judge(label=LABEL_NEUTRAL, enabled=False).reason == REASON_DISABLED


def test_disabled_yields_no_correction_at_all() -> None:
    result = _judge(label=LABEL_WONDERFUL, enabled=False)
    assert not result.applied
    assert result.reason == REASON_DISABLED


# ---------------------------------------------------------------------------
# model 层：加法 + 夹取，无策略
# ---------------------------------------------------------------------------


def test_apply_judgment_only_adds_and_clamps() -> None:
    stats = Stats(affection=20.0, mood=60.0, health=70.0, satiety=50.0, energy=50.0)
    updated = apply_judgment(stats, {"mood": 1.5, "health": 0.5, "affection": 0.3})
    assert updated.mood == pytest.approx(61.5)
    assert updated.health == pytest.approx(70.5)
    assert updated.affection == pytest.approx(20.3)
    assert updated.satiety == stats.satiety and updated.energy == stats.energy


def test_apply_judgment_clamps_at_the_bounds_and_ignores_unknown_axes() -> None:
    stats = Stats(affection=99.5, mood=0.5, health=99.0, satiety=50.0, energy=50.0)
    updated = apply_judgment(stats, {"mood": -5.0, "health": 5.0, "affection": 5.0, "nonsense": 3.0})
    assert updated.mood == 0.0
    assert updated.health == 100.0
    assert updated.affection == 100.0
    assert updated.satiety == 50.0 and updated.energy == 50.0


def test_apply_judgment_with_nothing_is_a_identity() -> None:
    stats = Stats(affection=20.0, mood=60.0, health=70.0)
    assert apply_judgment(stats, None) is stats
    assert apply_judgment(stats, {}) is stats


# ---------------------------------------------------------------------------
# 入口接线：our_life_judge
# ---------------------------------------------------------------------------


def _shard_payload(*, mood: float = 60.0, now: float, last_touch_at: float | None = None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "stats": {"affection": 30.0, "mood": mood, "health": 70.0, "satiety": 50.0, "energy": 50.0},
        "last_decay_at": now,
        "last_touch_at": last_touch_at if last_touch_at is not None else now - 60.0,
        "streak_days": 3,
        "milestones": [],
        "neglect_days_applied": 0.0,
        "session_turns": 0,
        "inject_timestamps": [],
        "inject_history": [],
        "respond_timestamps": [],
        "seen_conversation_ids": [],
        "hour_histogram": [0] * 24,
        "updated_at": now,
    }


def test_judge_tool_rejects_when_the_master_switch_is_off(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """总开关关着时她自己说"没开"——与 `our_life_company` 的语义一致，不撒谎说别的。"""
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["ok"] is False
    assert result["reason"] == "judgment_disabled"


def test_judge_tool_records_and_queues_without_touching_stats_directly(
    make_plugin: Any, run_async: Any, make_config: Any
) -> None:
    """工具只**记账 + 排队**：数值改动留给 tick，保证走在衰减/耦合同一条链上。"""
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)

    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["ok"] is True
    assert result["reason"] == REASON_OK
    assert "mood" not in result and "health" not in result  # 绝不把数值回传给模型

    # 落盘的台账前进了，但 stats 一动没动（等 tick）
    payload = host.store.data[SHARD_KEY]
    assert payload["last_judgment_at"] > 0.0
    assert payload["judgment_count_today"] == 1
    assert payload["judgment_used_add"] > 0.0
    assert payload["stats"]["mood"] == pytest.approx(60.0)
    assert plugin._pending_judgments["灵"]["mood"] > 0.0
    assert plugin._pending_judgments["灵"]["_label"] == LABEL_WONDERFUL


def test_judge_tool_spends_a_budget_once_per_interaction(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """连续两次判断：第二次被闸门 ④ 挡下（同一次互动只能消费一次）。"""
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)

    first = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    second = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert first["reason"] == REASON_OK
    assert second["reason"] == REASON_NO_INTERACTION
    assert second["ok"] is False
    # 但两次都记了账：否则 last_judgment_at 不前进，一轮里能无限重试
    assert host.store.data[SHARD_KEY]["judgment_count_today"] == 2


def test_judge_tool_without_a_touch_reports_needs_interaction(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    payload = _shard_payload(now=now)
    payload["last_touch_at"] = None
    host.store.data[SHARD_KEY] = payload

    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["ok"] is False
    assert result["reason"] == REASON_NO_INTERACTION


def test_judge_tool_never_raises_on_garbage_input(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """模型传垃圾是常态：工具必须收敛，绝不抛异常（抛了会被框架转成错给模型看）。"""
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)

    for bad in ({"label": "ecstatic"}, {"label": 123}, {"label": None}, {"label": ["x"]}, {}):
        result = run_async(plugin.our_life_judge(_ctx={"lanlan_name": "灵"}, **bad))
        assert isinstance(result, dict)
        assert result["label"] == LABEL_NEUTRAL
        assert result["ok"] is False
        assert result["reason"] == REASON_SILENT


def test_judge_tool_reports_missing_role(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    host.store.data["ourlife@雪"] = _shard_payload(now=time.time())
    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL))
    assert result["ok"] is False
    assert result["reason"] == "invalid_lanlan"


def test_judge_tool_respects_the_feedback_switch(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """`[our_life.feedback].enabled = false` 时工具自证关闭，而不是静默照收。"""
    config = _enabled_config()
    config["our_life"]["feedback"] = {"enabled": False}
    plugin, host = make_plugin(config=config)
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["ok"] is False
    assert result["reason"] == "judgment_disabled"


def test_judge_tool_does_not_persist_any_conversation_text(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """隐私门：判断台账只能有标签/时刻/幅度，**不能混进任何自由文本**。

    工具签名里刻意只有一个枚举 + 一个数字，就是为了让"模型把对话原文写进
    持久化状态"这条路根本不存在。这里把整份 payload 序列化后逐个字段核对。
    """
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.our_life_judge(label=LABEL_HURT, _ctx={"lanlan_name": "灵"}))

    payload = host.store.data[SHARD_KEY]
    for entry in payload["judgment_history"]:
        assert set(entry) == {"at", "label", "applied"}, f"unexpected keys in ledger entry: {sorted(entry)}"
        assert isinstance(entry["label"], str)
        assert isinstance(entry["at"], (int, float))
        assert isinstance(entry["applied"], (int, float))


# ---------------------------------------------------------------------------
# tick 接线：修正量与注入
# ---------------------------------------------------------------------------


def test_tick_applies_the_pending_judgment(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """排队一条判断后跑下一拍 tick：数值真的涨了，队列被消费，且涨得比一次互动少。

    刻意走**真实顺序**（先有一轮真对话 → tick 写入 `last_touch_at` → 她判断 →
    下一拍施加）：手工造分片会把 `last_touch_at` 编成墙钟时间，而真实值是**总线记录
    自带的时间戳**，两者不是同一个时钟源。
    """
    now = time.time()
    plugin, host = make_plugin(
        config=make_config(data=_enabled_config()),
        records=[_conversation("c1", now, "灵")],
    )
    run_async(plugin.on_startup())
    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())  # 第一拍：结算这轮真实互动，写入 last_touch_at
    before = host.store.data[SHARD_KEY]["stats"]["mood"]

    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["reason"] == REASON_OK
    assert "灵" in plugin._pending_judgments

    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())  # 第二拍：消费判断

    after = host.store.data[SHARD_KEY]["stats"]["mood"]
    assert after > before, "the queued judgment must be applied by the next tick"
    # 修正量必须小于一次真实互动（这是闸门 ① 的端到端体现）
    assert after - before < _GROWTH.turn_mood_gain
    # 队列已被消费：同一拍不会重复施加
    assert "灵" not in plugin._pending_judgments


def test_tick_consumes_a_pending_judgment_exactly_once(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))

    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())
    first = host.store.data[SHARD_KEY]["stats"]["mood"]

    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())
    second = host.store.data[SHARD_KEY]["stats"]["mood"]
    # 第二拍只应有惰性衰减，不该再涨（判断只消费一次）
    assert second <= first


def test_tick_emits_a_judgment_injection(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    host.pushed.clear()

    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())

    triggers = [item.get("metadata", {}).get("trigger") for item in host.pushed]
    assert TRIGGER_JUDGMENT in triggers
    # 注入契约：用户看不见（visibility 为空），不主动打断（read）
    judged = [item for item in host.pushed if item.get("metadata", {}).get("trigger") == TRIGGER_JUDGMENT]
    for item in judged:
        assert item["visibility"] == []
        assert item["ai_behavior"] == "read"
        assert item["coalesce_key"] == f"our_life:{TRIGGER_JUDGMENT}"


def test_neutral_judgment_does_not_emit_an_injection(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """"没什么特别感觉"不该变成一句空话去挤占 prompt。"""
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.our_life_judge(label=LABEL_NEUTRAL, _ctx={"lanlan_name": "灵"}))
    host.pushed.clear()

    plugin._last_tick_at = 0.0
    run_async(plugin.on_tick())

    triggers = [item.get("metadata", {}).get("trigger") for item in host.pushed]
    assert TRIGGER_JUDGMENT not in triggers


def test_disabled_master_switch_also_disables_feedback(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    """`[our_life].enabled = false` 是 fail-closed 总闸：反馈闭环必须一起停。"""
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config, {"our_life": {"enabled": False}})
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["ok"] is False
    assert result["reason"] == "judgment_disabled"


# ---------------------------------------------------------------------------
# 跨天与持久化
# ---------------------------------------------------------------------------


def test_judgment_budget_resets_on_a_new_day(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    payload = _shard_payload(now=now)
    payload["judgment_day"] = "1999-01-01"
    payload["judgment_used_add"] = 99.0
    payload["judgment_count_today"] = 9
    payload["last_judgment_at"] = 1.0
    payload["last_touch_at"] = now - 60.0
    host.store.data[SHARD_KEY] = payload

    result = run_async(plugin.our_life_judge(label=LABEL_WONDERFUL, _ctx={"lanlan_name": "灵"}))
    assert result["reason"] == REASON_OK
    stored = host.store.data[SHARD_KEY]
    assert stored["judgment_count_today"] == 1  # 归零后重新计
    assert stored["judgment_used_add"] < 99.0


def test_v2_shard_is_read_with_v3_feedback_defaults() -> None:
    """v0.2.0 的分片（没有反馈台账字段）必须能直接读进来，不用迁移脚本。"""
    from our_life.services.state import ShardState

    now = time.time()
    legacy = _shard_payload(now=now)
    for key in (
        "judgment_day",
        "judgment_used_add",
        "judgment_used_subtract",
        "judgment_count_today",
        "last_judgment_at",
        "judgment_history",
    ):
        legacy.pop(key, None)

    state = ShardState.from_payload("灵", legacy, now=now)
    assert state.judgment_day == ""
    assert state.judgment_used_add == 0.0
    assert state.judgment_used_subtract == 0.0
    assert state.judgment_count_today == 0
    assert state.last_judgment_at is None
    assert state.judgment_history == ()
    # 数值本身照旧读出来（不是被顺手清空）
    assert state.stats.mood == pytest.approx(60.0)


def test_corrupt_judgment_fields_fall_back_individually() -> None:
    from our_life.services.state import ShardState

    now = time.time()
    payload = _shard_payload(now=now)
    payload["judgment_used_add"] = "not-a-number"
    payload["judgment_count_today"] = None
    payload["judgment_history"] = [{"at": 1.0, "label": LABEL_GOOD}, "junk", 5]

    state = ShardState.from_payload("灵", payload, now=now)
    assert state.judgment_used_add == 0.0
    assert state.judgment_count_today == 0
    assert state.judgment_history == ({"at": 1.0, "label": LABEL_GOOD},)


def test_feedback_view_reports_remaining_budget_without_leaking_text(
    make_plugin: Any, run_async: Any, make_config: Any
) -> None:
    plugin, host = _enabled_plugin(make_plugin, run_async, make_config)
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.our_life_judge(label=LABEL_GOOD, _ctx={"lanlan_name": "灵"}))

    payload = run_async(plugin.dashboard_context(_ctx={"lanlan_name": "灵"}))
    feedback = payload["runtime"]["feedback"]
    assert feedback["enabled"] is True
    assert feedback["count_today"] == 1
    # 正负两本账各自给出"还剩多少"，面板据此显示额度
    assert feedback["daily_add_points"] == pytest.approx(_FEEDBACK.daily_add_points)
    assert feedback["remaining_subtract"] == pytest.approx(_FEEDBACK.daily_subtract_points)
    assert feedback["remaining_add"] < feedback["daily_add_points"]


def test_stat_names_stay_the_panel_order() -> None:
    """反馈闭环新增了三轴以外的读数，但面板数值顺序不能被动到。"""
    assert STAT_NAMES == ("energy", "satiety", "mood", "health", "affection")


def _enabled_config() -> dict[str, Any]:
    """总开关打开的最小配置（其余走 `OurLifeSettings` 的默认值）。"""
    return {"our_life": {"enabled": True}}


def _conversation(conversation_id: str, timestamp: float, lanlan: str, turn_type: str = "user") -> dict[str, Any]:
    """造一条总线轮次记录（形状与宿主 `bus.conversations` 一致）。"""
    return {
        "conversation_id": conversation_id,
        "timestamp": timestamp,
        "metadata": {"lanlan_name": lanlan, "turn_type": turn_type},
    }


def _enabled_plugin(
    make_plugin: Any, run_async: Any, make_config: Any, config: dict[str, Any] | None = None
) -> Any:
    """构造插件并**装配配置**（`on_startup` 是唯一把配置读进 `_settings` 的路径）。

    与 `test_entries.py` 同一惯例：总开关默认关，所有需要"她真的在过日子"的用例
    都必须先跑一次 startup，否则测的是"冻结态"而不是被测行为。
    配置通道必须是 `FakeConfig`（异常式接口），直接塞 dict 会让 `_reload_settings`
    静默退回默认值——那正是本插件刻意设计的容错，但会让测试变成假的绿。
    """
    plugin, host = make_plugin(config=make_config(data=config or _enabled_config()))
    run_async(plugin.on_startup())
    return plugin, host
