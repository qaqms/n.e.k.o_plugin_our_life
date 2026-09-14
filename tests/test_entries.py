"""入口与投递面门：角色归属、参数校验、开关语义、tick 结算与注入。

最要紧的一条：**角色归属只认本次调用注入的 `_ctx["lanlan_name"]`**。
`ctx._current_lanlan` 是上一次调用残留下来的脏值（jukebox_controller 源码里明确
写了不能用它兜底），一旦被当成权威来源，A 角色的互动就会记到 B 角色头上。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from our_life.core.configuration import DecaySettings
from our_life.core.model import Stats, apply_decay, tier_transitions

SHARD_KEY = "ourlife@灵"


def _shard_payload(*, affection: float = 30.0, mood: float = 60.0, health: float = 70.0, now: float) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "stats": {"affection": affection, "mood": mood, "health": health},
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


# ---------------------------------------------------------------------------
# 角色归属（脏值陷阱）
# ---------------------------------------------------------------------------


def test_lanlan_comes_from_the_injected_ctx(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    # 故意放一个"别的角色"的脏值在主机属性上
    plugin.ctx._current_lanlan = "别人"
    result = run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))
    assert result.is_ok()
    assert result.value["lanlan"] == "灵"


def test_dirty_current_lanlan_is_never_used(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    host.store.data["ourlife@雪"] = _shard_payload(now=now)
    plugin.ctx._current_lanlan = "别人"  # 脏值：只能被忽略

    result = run_async(plugin.status_entry())  # 没有 _ctx
    assert not result.is_ok()
    assert str(result.error) == "invalid_lanlan"


def test_single_shard_is_used_as_fallback(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    result = run_async(plugin.status_entry())
    assert result.is_ok()
    assert result.value["lanlan"] == "灵"


# ---------------------------------------------------------------------------
# 入口参数校验
# ---------------------------------------------------------------------------


def test_status_reports_loaded_code_and_tiers(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(affection=85.0, mood=15.0, health=45.0, now=now)
    result = run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))
    value = result.value
    assert value["note"] == "stats_loaded"
    assert value["tiers"] == {"affection": "bonded", "mood": "sulking", "health": "fair"}
    assert value["streak_days"] == 3


def test_tune_rejects_unknown_stat(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    result = run_async(plugin.tune_entry(stat="karma", value=50.0, _ctx={"lanlan_name": "灵"}))
    assert not result.is_ok()
    assert str(result.error) == "invalid_stat"


@pytest.mark.parametrize("bad", ["abc", None, True, float("nan")])
def test_tune_rejects_non_numeric_value(make_plugin: Any, run_async: Any, bad: Any) -> None:
    plugin, _host = make_plugin()
    result = run_async(plugin.tune_entry(stat="mood", value=bad, _ctx={"lanlan_name": "灵"}))
    assert not result.is_ok()
    assert str(result.error) == "invalid_value"


def test_tune_clamps_and_persists(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    result = run_async(plugin.tune_entry(stat="mood", value=180.0, _ctx={"lanlan_name": "灵"}))
    assert result.value["note"] == "stat_updated"
    assert result.value["value"] == 100.0
    assert host.store.data[SHARD_KEY]["stats"]["mood"] == 100.0


# ---------------------------------------------------------------------------
# 总开关（fail-closed）
# ---------------------------------------------------------------------------


def test_switch_writes_config_and_updates_settings(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    result = run_async(plugin.switch_entry(enabled=True, _ctx={"lanlan_name": "灵"}))
    assert result.value == {"note": "enabled", "enabled": True}
    assert ("our_life.enabled", True) in host.config.writes
    assert plugin._settings.enabled is True


def test_switch_reports_config_failure(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, _host = make_plugin(config=make_config(set_error=RuntimeError("boom")))
    result = run_async(plugin.switch_entry(enabled=True, _ctx={"lanlan_name": "灵"}))
    assert not result.is_ok()
    assert str(result.error) == "config_unavailable"


def test_switch_rejects_non_boolean(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    result = run_async(plugin.switch_entry(enabled="yes", _ctx={"lanlan_name": "灵"}))
    assert not result.is_ok()
    assert str(result.error) == "invalid_value"


def test_disabled_freezes_stats_and_sends_nothing(
    make_plugin: Any, run_async: Any, conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(
        config=_cfg(enabled=False),
        records=[conversation("c1", now, "灵", "user")],
    )
    host.store.data[SHARD_KEY] = _shard_payload(affection=30.0, mood=60.0, health=70.0, now=now - 10_000.0)

    run_async(plugin.on_startup())
    persisted_after_startup = dict(host.store.data[SHARD_KEY])
    result = run_async(plugin.on_tick())
    assert result.value["status"] == "tick_done"

    payload = host.store.data[SHARD_KEY]
    # 冻结 + 不写盘：整份持久化状态与启动后逐字段一致（禁用期不该每拍写一次 store）
    assert payload == persisted_after_startup
    assert payload["stats"] == {"affection": 30.0, "mood": 60.0, "health": 70.0}
    # 但内存里的折算基准点要往前推，免得重新打开时补算一大段衰减
    assert plugin._store.cached["灵"].last_decay_at == now
    assert host.pushed == []


def test_startup_normalizes_timestamp_while_disabled(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨重启的冻结语义：关着开关放几天再打开，不该补算整段衰减。"""
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(config=_cfg(enabled=False))
    host.store.data[SHARD_KEY] = _shard_payload(affection=30.0, mood=60.0, health=70.0, now=now - 500_000.0)

    run_async(plugin.on_startup())

    payload = host.store.data[SHARD_KEY]
    assert payload["last_decay_at"] == now  # 启动时归一化并落盘一次
    assert payload["stats"]["mood"] == 60.0  # 数值依旧冻结


# ---------------------------------------------------------------------------
# tick 结算
# ---------------------------------------------------------------------------


def test_tick_settles_interaction(
    make_plugin: Any, run_async: Any, conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(
        config=_cfg(enabled=True),
        records=[conversation("c1", now, "灵", "user")],
    )
    host.store.data[SHARD_KEY] = _shard_payload(affection=30.0, mood=50.0, health=70.0, now=now)

    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    payload = host.store.data[SHARD_KEY]
    assert payload["stats"]["mood"] > 50.0  # 互动给了成长
    assert payload["last_touch_at"] == now
    assert payload["streak_days"] == 1
    assert "c1" in payload["seen_conversation_ids"]


def test_tick_ignores_other_characters_turns(
    make_plugin: Any, run_async: Any, conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(
        config=_cfg(enabled=True),
        records=[conversation("mine", now, "灵", "user"), conversation("theirs", now, "雪", "user")],
    )
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    host.store.data["ourlife@雪"] = _shard_payload(now=now)

    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    assert host.store.data[SHARD_KEY]["last_touch_at"] == now
    # 雪 那一份不该吃到"灵"的轮次，也不该被标记已见
    assert "theirs" not in host.store.data[SHARD_KEY]["seen_conversation_ids"]


def test_tick_survives_bus_failure(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(config=_cfg(enabled=True))
    host.store.data[SHARD_KEY] = _shard_payload(now=now)

    async def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("bus down")

    monkeypatch.setattr(host.bus.conversations, "get", _boom)
    run_async(plugin.on_startup())
    result = run_async(plugin.on_tick())
    assert result.value["status"] == "tick_done"  # 降级而不是崩


def test_tick_survives_store_failure(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch, make_store: Any
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, _host = make_plugin(config=_cfg(enabled=True), store=make_store(available=False))
    run_async(plugin.on_startup())
    result = run_async(plugin.on_tick())
    assert result.value["status"] == "tick_done"


def test_crisis_causes_a_spoken_injection(
    make_plugin: Any, run_async: Any, conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(
        config=_cfg(enabled=True),
        records=[conversation("c1", now, "灵", "user")],
    )
    host.store.data[SHARD_KEY] = _shard_payload(affection=40.0, mood=5.0, health=70.0, now=now)

    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    assert host.pushed, "危机档 + 新互动应当触发一次注入"
    message = host.pushed[-1]
    assert message["visibility"] == []
    assert message["ai_behavior"] == "respond"  # 危机让她主动开口
    assert message["target_lanlan"] == "灵"
    assert message["source"] == "our_life"
    text = message["parts"][0]["text"]
    assert "{MASTER_NAME}" in text
    assert str(int(host.store.data[SHARD_KEY]["stats"]["mood"])) not in text or "心情" in text
    assert host.store.data[SHARD_KEY]["inject_history"][-1]["trigger"] == "crisis"


def test_quiet_tier_change_does_not_speak(
    make_plugin: Any, run_async: Any, conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(
        config=_cfg(enabled=True),
        records=[conversation("c1", now, "灵", "user")],
    )
    # 59 → 跨到 60（calm→happy）：非危机，应静默注入（read）
    host.store.data[SHARD_KEY] = _shard_payload(affection=40.0, mood=58.0, health=70.0, now=now)
    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    if host.pushed:
        assert host.pushed[-1]["ai_behavior"] == "read"


def test_first_decay_tick_sends_nothing_when_nothing_really_happened(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机复现门（集成层）：默认好评 20.0 压着分界线，第一拍衰减必须**不注入**。

    这条门盯的是 `__init__.py` 的接线：模型层已经有了带迟滞的判据
    （`eventful_tier_transitions`），但只要 tick 里还写着硬比较的 `tier_transitions`，
    真机上就会在第一次心跳里凭空发一条 `tier_change`（真机 store 的 inject_history 里
    躺着一条 `affection=stranger` 的 tier_change，就是这么来的）。

    这里没有任何新互动（总线是空的），所以"有理由注入"只可能来自亚分噪声。
    """
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    plugin, host = make_plugin(config=_cfg(enabled=True))  # 总线为空：没有新互动
    # 默认数值原样落盘，`last_decay_at` 设在 30 秒前 —— 真机上那一拍正是这个间隔
    host.store.data[SHARD_KEY] = _shard_payload(affection=20.0, now=now - 30.0)

    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    payload = host.store.data[SHARD_KEY]
    assert 19.99 < payload["stats"]["affection"] < 20.0  # 前提：确实被折过分界线一点点
    assert host.pushed == [], "亚分噪声不该触发注入"
    assert payload["inject_history"] == [], "注入历史里不该有记录"
    assert payload["inject_timestamps"] == []


def test_first_decay_tick_would_inject_with_a_hard_comparison(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一条门的前提校验：这一拍**确实**把数值折过了分界线。

    把判据换回硬比较（`core.model.tier_transitions`）就能看出档位"变了"——
    这正是修复前真机 store 里那条 `tier_change` 的来源。两条门合起来才说明
    "接线用的是带迟滞的判据，而不是硬比较"。
    """
    before = Stats(affection=20.0)
    after = apply_decay(before, elapsed_hours=30.0 / 3600.0, decay=DecaySettings())
    assert before.affection == 20.0
    assert after.affection < 20.0
    assert tier_transitions(before, after) == (("affection", "acquainted", "stranger"),)


def test_status_bootstraps_a_shard_so_the_tick_has_a_role(make_plugin: Any, run_async: Any) -> None:
    """冷启动关键路径：tick 是后台定时器、没有 `_ctx`，只能靠分片名单决定结算谁。

    面板/入口被碰过就落一个默认分片，tick 从此有名单——否则冷装机上（总线记录若不带
    角色名）插件会一直空转、数值永远不动。
    """
    plugin, host = make_plugin()
    assert host.store.data == {}
    run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))
    assert SHARD_KEY in host.store.data
    assert plugin._bootstrapped == {"灵"}


def test_bootstrap_overwrites_at_most_once_per_run(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))
    host.store.data[SHARD_KEY]["stats"]["mood"] = 33.0  # 人为改一下：再 bootstrap 就会被覆盖
    run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))
    assert host.store.data[SHARD_KEY]["stats"]["mood"] == 33.0


# ---------------------------------------------------------------------------
# 面板上下文
# ---------------------------------------------------------------------------


def test_dashboard_reports_invalid_lanlan_without_shards(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    payload = run_async(plugin.dashboard_context())
    assert payload["state"] is None
    assert payload["error_code"] == "invalid_lanlan"
    assert payload["recent_injections"] == []
    assert payload["enabled"] is False


def test_dashboard_payload_shape(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    payload = run_async(plugin.dashboard_context(_ctx={"lanlan_name": "灵"}))
    assert payload["lanlan"] == "灵"
    assert payload["enabled"] is False
    assert payload["store_available"] is True
    assert set(payload["state"]) >= {"mood", "health", "affection", "tiers", "streak_days", "gap_hours"}
    assert len(payload["hours"]) == 24
    assert payload["config"]["tick_seconds"] == 30


def test_dashboard_survives_store_unavailable(
    make_plugin: Any, run_async: Any, make_store: Any
) -> None:
    plugin, _host = make_plugin(store=make_store(available=False))
    payload = run_async(plugin.dashboard_context())
    assert payload["store_available"] is True  # 通道对象在，只是读写会失败
    assert payload["state"] is None or isinstance(payload["state"], dict)


# ---------------------------------------------------------------------------
# store 通道整个缺席（`store_unavailable` 的接线门）
# ---------------------------------------------------------------------------
#
# 边界刻意划在这里：**只有"通道对象不在"才算 `store_unavailable`**。
# `get`/`set` 返回 `Err` 的瞬时降级不算——那是本插件的既定容错契约
# （`services/state.py` 模块 docstring：持久化失败只降级、不抛给调用方），
# 把它也算进来的话，每拍都可能发生的瞬时故障会让面板横幅一直挂着。
# 上面那条 `test_dashboard_survives_store_unavailable` 正是这条边界的守门测试。


def test_store_available_reflects_the_channel_object_only(make_plugin: Any, make_store: Any) -> None:
    plugin, _host = make_plugin()
    assert plugin._store.store_available is True
    plugin.store = None
    assert plugin._store.store_available is False
    assert plugin._store.store_available is False  # 只读探测，不产生副作用


def test_status_reports_store_unavailable_instead_of_playing_healthy(
    make_plugin: Any, run_async: Any
) -> None:
    """通道缺席时 `status` 必须如实回码，不能假装"一切正常"。

    真机上这条通路的意义：数值只在内存里、重启就丢；面板拿到 `store_unavailable`
    才能显示横幅。修复前它无论如何都回 `stats_loaded`，等于对用户说谎。
    """
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    plugin.store = None  # 模拟宿主没给出 store 通道
    result = run_async(plugin.status_entry(_ctx={"lanlan_name": "灵"}))

    value = result.value
    assert value["note"] == "store_unavailable"
    assert value["store_available"] is False
    # 数值照旧可读（内存缓存），只是活不过重启——如实回码不等于拒绝服务
    assert value["tiers"]["mood"] in {"sulking", "low", "calm", "happy", "elated"}


def test_tune_and_reset_admit_when_nothing_was_persisted(make_plugin: Any, run_async: Any) -> None:
    """`tune` / `reset` 在通道缺席时也要如实回码——"改成功了"是句假话。"""
    plugin, _host = make_plugin()
    plugin.store = None

    tuned = run_async(plugin.tune_entry(stat="mood", value=80.0, _ctx={"lanlan_name": "灵"}))
    assert tuned.value["note"] == "store_unavailable"
    assert tuned.value["value"] == 80.0  # 内存里确实改了

    reset = run_async(plugin.reset_entry(_ctx={"lanlan_name": "灵"}))
    assert reset.value["note"] == "store_unavailable"


def test_dashboard_raises_the_banner_code_only_when_the_channel_is_absent(
    make_plugin: Any, run_async: Any
) -> None:
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())

    healthy = run_async(plugin.dashboard_context(_ctx={"lanlan_name": "灵"}))
    assert healthy["store_available"] is True
    assert "error_code" not in healthy  # 通道在 ⇒ 不挂横幅

    plugin.store = None
    broken = run_async(plugin.dashboard_context(_ctx={"lanlan_name": "灵"}))
    assert broken["store_available"] is False
    assert broken["error_code"] == "store_unavailable"


def test_switch_still_works_without_the_store_channel(
    make_plugin: Any, run_async: Any
) -> None:
    """总开关走的是**配置层**，与数据 store 是两条通道——store 缺席不许挡住开关。

    这条门钉住一个刻意的"不改语义"决定：一度想在 `switch` 上用
    `store_available` 挡写，但那会是误报（配置写不下该看 `config_unavailable`）。
    """
    plugin, _host = make_plugin()
    plugin.store = None
    result = run_async(plugin.switch_entry(enabled=True, _ctx={"lanlan_name": "灵"}))
    assert result.is_ok()
    assert result.value["note"] == "enabled"


# ---------------------------------------------------------------------------
# LLM 工具
# ---------------------------------------------------------------------------


def test_feel_tool_returns_state_without_numbers_in_text(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    payload = run_async(plugin.our_life_feel(_ctx={"lanlan_name": "灵"}))
    assert payload["tiers"]["mood"] in {"sulking", "low", "calm", "happy", "elated"}
    assert "{MASTER_NAME}" in payload["feeling"]


def test_feel_tool_without_any_shard(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    payload = run_async(plugin.our_life_feel())
    assert payload["reliable"] is False
    assert payload["reason"] == "invalid_lanlan"


def test_company_tool_is_rate_limited(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin(config=_cfg(enabled=True))
    now = time.time()
    host.store.data[SHARD_KEY] = _shard_payload(now=now)
    run_async(plugin.on_startup())  # 装配配置（总开关默认关，必须走一次 startup）

    first = run_async(plugin.our_life_company(reason="想他了", _ctx={"lanlan_name": "灵"}))
    assert first["ok"] is True
    second = run_async(plugin.our_life_company(reason="又想了", _ctx={"lanlan_name": "灵"}))
    assert second["ok"] is False
    assert second["reason"] == "company_cooldown"


def test_company_tool_refuses_when_disabled(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin(config=_cfg(enabled=False))
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    payload = run_async(plugin.our_life_company(_ctx={"lanlan_name": "灵"}))
    assert payload["ok"] is False
    # 语义要分明：不是"冷却中"，而是总开关关着
    assert payload["reason"] == "not_enabled"


# ---------------------------------------------------------------------------
# 隐私门
# ---------------------------------------------------------------------------


def test_conversation_text_is_never_persisted(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bus.conversations` 的记录里带 `content`（对话正文）。

    本插件只消费 id / 时间戳 / 角色 / 轮次类型四个非正文字段；这条门保证对话原文
    不会顺着 tick 流进持久化状态（也不该出现在注入记录里）。
    """
    import json

    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    secret = "这是绝不该被落盘的对话原文"
    record = {
        "conversation_id": "c-secret",
        "timestamp": now,
        "content": secret,
        "lanlan_name": "灵",
        "turn_type": "user",
    }
    plugin, host = make_plugin(config=_cfg(enabled=True), records=[record])
    host.store.data[SHARD_KEY] = _shard_payload(now=now)

    run_async(plugin.on_startup())
    run_async(plugin.on_tick())

    dumped = json.dumps(host.store.data, ensure_ascii=False)
    assert secret not in dumped
    assert host.store.data[SHARD_KEY]["seen_conversation_ids"] == ["c-secret"]  # 只留 id


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_startup_warm_loads_persisted_shards(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    result = run_async(plugin.on_startup())
    assert result.value["status"] == "ready"
    assert result.value["shards"] == 1
    assert plugin._store.known_lanlans() == ("灵",)


def test_shutdown_saves_loaded_shards(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    host.store.data[SHARD_KEY] = _shard_payload(now=time.time())
    run_async(plugin.on_startup())
    result = run_async(plugin.on_shutdown())
    assert result.value["status"] == "stopped"
    assert result.value["saved"] == 1


def test_config_change_reloads_settings(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin(config=_cfg(enabled=True))
    run_async(plugin.on_startup())
    assert plugin._settings.enabled is True
    result = run_async(plugin.on_config_change())
    assert result.value["status"] == "config_updated"


def test_startup_survives_unreadable_config(make_plugin: Any, run_async: Any, make_config: Any) -> None:
    plugin, _host = make_plugin(config=make_config(dump_error=RuntimeError("nope")))
    result = run_async(plugin.on_startup())
    assert result.value["status"] == "ready"
    assert plugin._settings.enabled is False  # 保持 fail-closed 默认


class _ToggleConfig:
    """只用于切换 `enabled` 的最小配置桩（本文件内部用；复杂场景走 conftest 夹具）。"""

    def __init__(self, enabled: bool):
        self.data = {"our_life": {"enabled": enabled}}
        self.writes: list[tuple[str, Any]] = []

    async def dump(self) -> dict[str, Any]:
        return self.data

    async def set(self, path: str, value: Any) -> None:
        self.writes.append((path, value))
        self.data.setdefault("our_life", {})["enabled"] = value


def _cfg(*, enabled: bool) -> Any:
    return _ToggleConfig(enabled=enabled)
