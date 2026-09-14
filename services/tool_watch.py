"""LLM 工具注册韧性：周期巡检 + 缺席重注册（v0.5.0）。

**病灶（宿主源码核实）**：``@llm_tool`` 只在插件启动时经 SDK 发一次
``LLM_TOOL_REGISTER`` IPC，fire-and-forget；那一刻 ``main_server`` 没起来
（"宿主起得比插件晚"）则注册在 user_plugin_server→main_server 一跳被
warning 掉且**不重试**（``plugin/server/messaging/llm_tool_registry.py`` 的
``register_remote_tool`` docstring 自己写着 "The plugin can re-register later"）。
``main_server`` 的 tool_registry 是 ``LLMSessionManager`` 的内存属性，它重启即全丢。
两种时序下她都会**静默失去** ``our_life_feel / our_life_company / our_life_judge``——
反馈闭环整条失效，且没有任何人会主动发现。

官方钦定的解法（``docs/zh-CN/plugins/tool-calling.md``「main_server 重启会发生什么」）：
"定期 ``GET /api/tools`` 检查自己的工具是否还在，不在就重新 register"。
``forever_companion`` 的 host_coord（300s）已证明这条是承重结构而非可选优化。

三条纪律：

1. **不可达 = 什么都不做**：main_server 还没起来时"全部缺席"是预期现象，
   盲重注册只是每轮三发注定失败的 POST；只有"可达、且逐个点名缺席"才补挂。
2. **补挂走 IPC 重发**（宿主侧 replace 语义、重复注册幂等），**不**走公开
   ``register_llm_tool`` 重调——名字已在 SDK 的 ``_llm_tools`` 里会撞
   ``EntryConflictError``；而"先 unregister 再 register"有本地条目已删、
   远端重注册又失败的窗口，比原地补挂更糟。``_notify_llm_tool_registered``
   是基类 protected 面：一律 ``getattr`` 取用，宿主哪天改名时降级为
   "心跳失效 + 日志一条"，不是崩溃。
3. **永不炸 tick**：查询失败 / 形状不认识 / 单个补挂抛错，全部降级为
   "这一拍什么也没发生"。形状不认识时按"全场无工具"处理——后果只是
   重发一轮注册（幂等、回环、每 5 分钟最多三次 POST），比"真缺席却不动"安全。

间隔固定为模块常量（300s），不进配置：它是可靠性机制的心跳参数，
不是行为参数——和衰减速率、分档阈值同等待遇（见 plugin.toml 的分层注释）。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any, Callable, Iterable, Mapping

__all__ = ["TOOL_WATCH_INTERVAL_SEC", "ToolWatch", "missing_tool_names"]

# 巡检间隔（秒）：与 forever_companion 实测过的兜底同量级。
# 太短 → main_server 正常时也每拍多一发回环 GET；太长 → "她没工具"的窗口拉长。
TOOL_WATCH_INTERVAL_SEC = 300.0

# 回环 GET 的超时（秒）：loopback 上 4s 已经算"对面不对劲"，别拿它卡 tick。
_HTTP_TIMEOUT_SEC = 4.0


def missing_tool_names(payload: Any, declared: Iterable[str]) -> list[str]:
    """从 ``GET /api/tools`` 的响应里数出**缺席**的 declared 工具名（纯函数）。

    实际形状（tool_router.py:555-575 + fc 实测）：
    ``{"ok": true, "tools_by_role": {"<role>": [{"name": ...}, ...]}}``；
    ``tools_by_role`` 缺席时把顶层当平铺 ``{role: [tools]}`` 兼容一次。
    认不出的形状按"一个工具都不在"处理（见模块 docstring 纪律 3），
    所以本函数返回**排序后的稳定列表**，调用方可直接逐名补挂。
    """
    wanted = sorted({name for raw in declared if (name := str(raw).strip())})
    if not wanted:
        return []
    present: set[str] = set()
    if isinstance(payload, Mapping):
        groups = payload.get("tools_by_role")
        if not isinstance(groups, Mapping):
            # 平铺兜底：顶层本身可能就是 {role: [tool, ...]}。
            groups = payload
        for tools in groups.values():
            if isinstance(tools, Iterable) and not isinstance(tools, (str, bytes)):
                for entry in tools:
                    if isinstance(entry, Mapping) and entry.get("name"):
                        present.add(str(entry["name"]))
    return [name for name in wanted if name not in present]


async def _default_fetch(url: str) -> dict[str, Any] | None:
    """stdlib urllib 拉一次 ``GET /api/tools``；任何失败返回 None（=不可达）。

    运行期才 import 宿主 ``config`` 拿端口（独立仓/测试环境没有它，回落 48911），
    与 fc 同一形态。URL 由调用方传入的部分只有 path——端口解析留在这里，
    测试用注入的 fetch 替身，完全不碰网络。
    """
    port = 48911
    try:
        from config import MAIN_SERVER_PORT

        port = int(MAIN_SERVER_PORT)
    except Exception:  # noqa: BLE001 - 独立仓态/测试态：缺省端口即可
        pass
    base = f"http://127.0.0.1:{port}{url}"

    def _do() -> dict[str, Any]:
        with urllib.request.urlopen(base, timeout=_HTTP_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}

    try:
        return await asyncio.to_thread(_do)
    except Exception:  # noqa: BLE001 - 不可达/超时/坏 JSON 统一降级为 None
        return None


class ToolWatch:
    """挂在 tick 上的低频巡检器。``maybe_run`` 每拍调用，内部按间隔自节流。"""

    def __init__(
        self,
        plugin: Any,
        *,
        logger: Any = None,
        fetch: Callable[[str], Any] | None = None,
        interval_sec: float = TOOL_WATCH_INTERVAL_SEC,
    ):
        self._plugin = plugin
        self._logger = logger
        # fetch 可注入（测试离线）；缺省走回环 GET。
        self._fetch = fetch or _default_fetch
        self._interval = max(30.0, float(interval_sec))
        # 0.0 = 进程起来第一拍（tick_seconds 后）立刻巡检，不等满一个间隔——
        # "宿主起得比插件晚"的竞态窗口要的是早发现。
        self._last_run_at = 0.0

    def _log(self, message: str, *args: Any, exc: bool = False) -> None:
        if self._logger is None:
            return
        try:
            (self._logger.exception if exc else self._logger.info)(message, *args)
        except Exception:  # noqa: BLE001 - 日志面坏掉不许影响巡检
            pass

    def _declared_names(self) -> list[str]:
        """本插件声明的工具名：走公开 ``list_llm_tools``，形状不认识就当没有。"""
        lister = getattr(self._plugin, "list_llm_tools", None)
        if not callable(lister):
            return []
        try:
            tools = lister()
        except Exception:  # noqa: BLE001
            return []
        names = [
            str(entry.get("name"))
            for entry in (tools or [])
            if isinstance(entry, Mapping) and entry.get("name")
        ]
        return sorted(set(names))

    def _reissue(self, missing: list[str]) -> int:
        """逐名重发 ``LLM_TOOL_REGISTER``（replace 幂等）。返回成功发出的条数。

        单名失败不许吃掉其余名字；基类 protected 面缺席（宿主改名/收口）时
        整体降级为 0 并记一条日志——心跳失效是体验退化，不是事故。
        """
        store = getattr(self._plugin, "_llm_tools", None)
        notify = getattr(self._plugin, "_notify_llm_tool_registered", None)
        if not isinstance(store, dict) or not callable(notify):
            self._log("our_life tool watch: SDK re-emit surface unavailable, skipping")
            return 0
        sent = 0
        for name in missing:
            meta = store.get(name)
            if meta is None:
                continue
            try:
                notify(meta)
                sent += 1
            except Exception:  # noqa: BLE001 - 单工具失败不影响其余
                self._log("our_life tool watch: re-emit failed for {}", name, exc=True)
        return sent

    async def maybe_run(self, *, now: float) -> dict[str, Any]:
        """机会式一拍巡检。返回值只为可观测性（tick 不消费、测试消费）。

        任何内部异常都在本方法内消化成 ``{"status": "watch_failed"}``：
        注册韧性坏掉不许把行为采样 / 衰减结算一起拖下水。
        """
        if now - self._last_run_at < self._interval:
            return {"status": "waiting"}
        declared = self._declared_names()
        if not declared:
            # 装饰器工具还没收集上（startup 未完成 / SDK 形状不认识）：
            # 没有核对对象，也不推进时钟——收集齐了下一拍就查。
            return {"status": "no_tools"}
        self._last_run_at = now
        try:
            payload = await self._fetch("/api/tools")
            if payload is None:
                # 不可达：等下一轮。不盲重注册（纪律 1），也不把时钟退回去——
                # 退回去会让"main_server 长期没起"变成每拍一发 GET 的追打。
                return {"status": "unreachable"}
            missing = missing_tool_names(payload, declared)
            if not missing:
                return {"status": "healthy"}
            reissued = self._reissue(missing)
            self._log(
                "our_life tool watch: re-registered {} missing llm tool(s): {}",
                reissued, ", ".join(missing),
            )
            return {"status": "repaired", "missing": missing, "reissued": reissued}
        except Exception:  # noqa: BLE001 - 永不炸 tick（纪律 3）
            self._log("our_life tool watch failed", exc=True)
            return {"status": "watch_failed"}
