"""v0.5.0 工具注册心跳门。

覆盖四类不变量：
1. **间隔自节流**：首拍即查（``_last_run_at=0``）、间隔内 waiting、满间隔再查；
   "没工具可查"（no_tools）**不推进时钟**——工具收集齐后下一拍就该查。
2. **不可达不盲挂**：fetch 返回 None → unreachable、零补挂；且时钟已推进，
   main_server 长期没起时不会退化成每拍追打。
3. **点名补挂**：只重发缺席的那几个；``tools_by_role`` 嵌套形状与平铺形状都要认；
   认不出的形状按"全场无工具"处理（宁可幂等重发，不可漏挂）。
4. **永不炸 tick / 永不连坐**：单名 notify 抛错不影响其余名字；SDK protected 面
   缺席时整体降级为 0；fetch 抛异常 → watch_failed 且不带出方法。
"""

from __future__ import annotations

from typing import Any

from our_life.services.tool_watch import TOOL_WATCH_INTERVAL_SEC, ToolWatch, missing_tool_names


class FakePlugin:
    """最小插件替身：公开面 ``list_llm_tools`` + protected 面 ``_llm_tools``/notify。"""

    def __init__(self, names: list[str], *, with_notify: bool = True):
        self.tools = [{"name": name} for name in names]
        self._llm_tools = {name: {"meta": name} for name in names}
        self.reemitted: list[Any] = []
        self.raise_for: set[str] = set()
        if with_notify:
            self._notify_llm_tool_registered = self._notify

    def list_llm_tools(self) -> list[dict[str, Any]]:
        return list(self.tools)

    def _notify(self, meta: Any) -> None:
        name = meta["meta"]
        if name in self.raise_for:
            raise RuntimeError(f"boom for {name}")
        # 记的是**原对象**（重发语义要求 meta 逐字传回基类），测试用 name 读回。
        self.reemitted.append(meta)


def _names(emitted: list[Any]) -> list[str]:
    return sorted(item["meta"] for item in emitted)


def _watch(plugin: Any, payload: Any, *, interval: float = TOOL_WATCH_INTERVAL_SEC) -> tuple[ToolWatch, list[str]]:
    """造一个离线巡检器：fetch 直接回给定 payload，并记录被请求的 URL。

    注意用 ``{"tools_by_role": {}}`` 表达"可达但全场无工具"——空 dict 会被
    形状门当成"认不出"，两者行为故意相同（都重发）但测试里要说清是哪一种。
    """
    urls: list[str] = []

    async def fetch(url: str):
        urls.append(url)
        return payload

    return ToolWatch(plugin, fetch=fetch, interval_sec=interval), urls


# ---------------------------------------------------------------------------
# 纯函数：形状与差集
# ---------------------------------------------------------------------------


def test_missing_uses_nested_tools_by_role_shape() -> None:
    payload = {"ok": True, "tools_by_role": {"小春": [{"name": "our_life_feel"}], "别的": [{"name": "x"}]}}
    assert missing_tool_names(payload, ["our_life_feel", "our_life_judge"]) == ["our_life_judge"]


def test_missing_accepts_flat_fallback_shape() -> None:
    payload = {"role_a": [{"name": "a"}, {"name": "b"}]}
    assert missing_tool_names(payload, ["a", "c"]) == ["c"]


def test_unrecognized_shape_means_everything_missing() -> None:
    # 认不出 = "一个工具都不在"：调用方幂等重发，比漏挂安全。
    assert missing_tool_names({"weird": 1}, ["b", "a"]) == ["a", "b"]
    assert missing_tool_names(None, ["a"]) == ["a"]


def test_empty_declared_returns_empty() -> None:
    assert missing_tool_names({}, []) == []
    assert missing_tool_names({}, ["", "  "]) == []


def test_interval_constant_is_pinned() -> None:
    # 与 fc 实测过的兜底同量级；改动它要有理由，这里钉住默认值。
    assert TOOL_WATCH_INTERVAL_SEC == 300.0


# ---------------------------------------------------------------------------
# 间隔与自节流
# ---------------------------------------------------------------------------


def test_first_run_checks_immediately_then_waits_interval(run_async: Any) -> None:
    plugin = FakePlugin(["our_life_feel"])
    watch, urls = _watch(plugin, {"tools_by_role": {"r": [{"name": "our_life_feel"}]}})
    first = run_async(watch.maybe_run(now=1000.0))
    assert first["status"] == "healthy" and urls == ["/api/tools"]
    # 间隔内：不再发请求、不推进时钟
    assert run_async(watch.maybe_run(now=1000.0 + TOOL_WATCH_INTERVAL_SEC - 1))["status"] == "waiting"
    assert len(urls) == 1
    # 满间隔：再查一次
    assert run_async(watch.maybe_run(now=1000.0 + TOOL_WATCH_INTERVAL_SEC))["status"] == "healthy"
    assert len(urls) == 2


def test_no_tools_does_not_advance_clock(run_async: Any) -> None:
    plugin = FakePlugin([])
    watch, urls = _watch(plugin, {"tools_by_role": {"r": [{"name": "our_life_feel"}]}})
    assert run_async(watch.maybe_run(now=1000.0))["status"] == "no_tools"
    assert urls == []
    # 时钟没被推进：工具收集齐后（间隔内！）下一拍就该查。
    plugin.tools = [{"name": "our_life_feel"}]
    plugin._llm_tools["our_life_feel"] = {"meta": "our_life_feel"}
    assert run_async(watch.maybe_run(now=1030.0))["status"] == "healthy"
    assert urls == ["/api/tools"]


# ---------------------------------------------------------------------------
# 不可达 / 补挂 / 降级
# ---------------------------------------------------------------------------


def test_unreachable_skips_reissue_but_keeps_clock(run_async: Any) -> None:
    plugin = FakePlugin(["our_life_feel"])
    watch, urls = _watch(plugin, None)
    assert run_async(watch.maybe_run(now=1000.0))["status"] == "unreachable"
    assert plugin.reemitted == []
    # 时钟已推进：main_server 长期没起时不会每拍追打
    assert run_async(watch.maybe_run(now=1010.0))["status"] == "waiting"
    assert len(urls) == 1


def test_repairs_only_missing_by_name(run_async: Any) -> None:
    plugin = FakePlugin(["our_life_feel", "our_life_judge", "our_life_company"])
    payload = {"tools_by_role": {"r": [{"name": "our_life_feel"}]}}
    watch, _urls = _watch(plugin, payload)
    result = run_async(watch.maybe_run(now=1000.0))
    assert result["status"] == "repaired"
    assert result["missing"] == ["our_life_company", "our_life_judge"]  # 排序稳定
    assert result["reissued"] == 2
    assert _names(plugin.reemitted) == ["our_life_company", "our_life_judge"]
    # 重发的是 SDK 里的 meta 对象本身（宿主按它拼 IPC payload）
    assert all(isinstance(meta, dict) for meta in plugin.reemitted)


def test_single_tool_failure_does_not_stop_the_rest(run_async: Any) -> None:
    plugin = FakePlugin(["a", "b", "c"])
    plugin.raise_for = {"b"}
    watch, _urls = _watch(plugin, {"tools_by_role": {}})
    result = run_async(watch.maybe_run(now=1000.0))
    assert result["status"] == "repaired"
    assert result["reissued"] == 2
    assert _names(plugin.reemitted) == ["a", "c"]


def test_missing_protected_surface_degrades_to_zero(run_async: Any) -> None:
    # 宿主哪天改名/收口 protected 面：心跳失效，不崩、不误伤其余流程。
    plugin = FakePlugin(["a"], with_notify=False)
    del plugin._llm_tools
    watch, _urls = _watch(plugin, {"tools_by_role": {}})
    result = run_async(watch.maybe_run(now=1000.0))
    assert result["status"] == "repaired"
    assert result["reissued"] == 0


def test_fetch_exception_is_swallowed(run_async: Any) -> None:
    plugin = FakePlugin(["a"])

    async def fetch(_url: str):
        raise OSError("pipe exploded")

    watch = ToolWatch(plugin, fetch=fetch)
    assert run_async(watch.maybe_run(now=1000.0))["status"] == "watch_failed"
