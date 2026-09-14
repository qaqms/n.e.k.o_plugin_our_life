"""宽松类型转换helper。

配置来自 TOML，用户手改后类型可能漂移（字符串数字、缺键、null、列表里混进非数字）。
core/ 是纯函数层，必须对这些输入保持韧性：一律回退到默认值，**不抛异常**，
避免一个手滑的配置项把整个插件 tick 打挂。

设计取舍：这里不做告警（core 层没有 logger），类型不符由调用方在装配时按需记录。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["as_bool", "as_float", "as_float_list", "as_int", "as_str"]


def as_bool(value: Any, default: bool) -> bool:
    """TOML 布尔；字符串按常见真值表解析，其余回退默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def as_str(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def as_float_list(value: Any, default: Sequence[float]) -> tuple[float, ...]:
    """数值列表；非法项跳过，整体为空时回退默认序列。"""
    if isinstance(value, (list, tuple)):
        out: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                continue
            parsed = as_float(item, float("nan"))
            if parsed == parsed:  # 过滤 NaN
                out.append(parsed)
        if out:
            return tuple(out)
    return tuple(float(x) for x in default)


def section(config: Mapping[str, Any] | None, *path: str) -> Mapping[str, Any]:
    """按点号路径取子表；任一层不是映射就返回空映射。"""
    current: Any = config or {}
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def as_int_list(value: Any, default: Sequence[int]) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                continue
            parsed = as_int(item, -1)
            if parsed >= 0:
                out.append(parsed)
        if out:
            return tuple(out)
    return tuple(int(x) for x in default)
