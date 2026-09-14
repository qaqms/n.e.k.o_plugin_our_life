"""总线行为采样：只读宿主 `bus.conversations`，聚合成"主人来过没有"。

只读快照 + 定时轮询是本插件唯一的行为数据来源（`conversations` 不支持 `watch()`）。
采样全部包在 try/except 里并降级为空结果：宿主升级改了总线形状时，
本插件的下场必须是"停止成长"，绝不可是"插件进程崩掉"。
"""

from __future__ import annotations

import inspect
from typing import Any, Mapping

from ..core.behavior import BehaviorSummary, empty_summary, select_new_turns, summarize_turns

__all__ = ["BehaviorSampler", "MAX_RECORDS"]

# 每拍最多读多少条轮次：够覆盖"两拍之间的新增"，又不至于每次拉全量历史
MAX_RECORDS = 200


class BehaviorSampler:
    def __init__(self, plugin: Any, *, logger: Any = None, max_records: int = MAX_RECORDS):
        self._plugin = plugin
        self._logger = logger
        self._max_records = max(10, int(max_records))

    async def fetch(self) -> tuple[Mapping[str, Any], ...]:
        """读一轮原始轮次记录；任何异常都降级为空元组。"""
        namespace = getattr(getattr(self._plugin, "bus", None), "conversations", None)
        getter = getattr(namespace, "get", None)
        if not callable(getter):
            return ()
        try:
            raw = await _maybe_await(getter(max_count=self._max_records))
        except Exception:
            self._log("bus.conversations.get failed", exc=True)
            return ()
        return _records_of(raw)

    def discover_lanlans(self, records: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
        """从轮次记录里发现出现过哪些角色卡（后台判归属的权威来源）。"""
        found: set[str] = set()
        for record in records:
            metadata = record.get("metadata")
            meta: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}
            name = record.get("lanlan_name") or meta.get("lanlan_name")
            if isinstance(name, str) and name.strip():
                found.add(name.strip())
        return tuple(sorted(found))

    def summarize_for(
        self, records: tuple[Mapping[str, Any], ...], *, lanlan: str, seen_ids: tuple[str, ...]
    ) -> BehaviorSummary:
        """挑出属于 `lanlan` 的新轮次并聚合。"""
        if not records:
            return empty_summary()
        turns, _new_ids = select_new_turns(
            records, lanlan=lanlan, seen_ids=seen_ids, fallback_lanlan=lanlan
        )
        if not turns:
            return empty_summary()
        return summarize_turns(turns)

    def _log(self, message: str, *, exc: bool = False) -> None:
        if self._logger is None:
            return
        try:
            if exc:
                self._logger.debug(message, exc_info=True)
            else:
                self._logger.debug(message)
        except Exception:
            pass


def _records_of(raw: Any) -> tuple[Mapping[str, Any], ...]:
    """把 SDK 的 bus 列表对象规范成 dict 元组（兼容 dump_records / items / 可迭代）。"""
    if raw is None:
        return ()
    dumper = getattr(raw, "dump_records", None)
    if callable(dumper):
        try:
            dumped = dumper()
            return _normalize(dumped)
        except Exception:
            pass
    items = getattr(raw, "items", None)
    if isinstance(items, (list, tuple)):
        return _normalize(items)
    if isinstance(raw, (list, tuple)):
        return _normalize(raw)
    return ()


def _normalize(value: Any) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for item in value if isinstance(value, (list, tuple)) else ():
        if isinstance(item, Mapping):
            out.append(item)
            continue
        dumper = getattr(item, "dump", None)
        if callable(dumper):
            try:
                dumped = dumper()
            except Exception:
                continue
            if isinstance(dumped, Mapping):
                out.append(dumped)
    return tuple(out)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
