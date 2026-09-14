"""行为统计：把宿主总线的对话轮次聚合成「主人来过没有」的事实。

数据面事实（源码核实，见 DESIGN.md 已知陷阱 §2）：
- `bus.conversations` 是**只读快照**且**不支持 `watch()`**——只有 messages / events / lifecycle 能 watch。
  所以行为采样只能定时轮询 + 用 `conversation_id` 去重。
- `bus.conversations` 的记录自带 `lanlan_name` 与 `turn_type`（user / assistant），
  这正是后台判定"数值属于哪个角色卡"的权威来源；`bus.messages` 的记录**没有**角色字段。

本模块保持纯函数：服务层负责把 SDK 记录规范成 Mapping，再交给这里聚合。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

__all__ = [
    "ASSISTANT_TURN_TYPES",
    "BehaviorSummary",
    "TurnRecord",
    "empty_summary",
    "gap_hours",
    "local_day",
    "normalize_turn",
    "select_new_turns",
    "summarize_turns",
]

USER_TURN_TYPES = frozenset({"user", "master", "human"})
ASSISTANT_TURN_TYPES = frozenset({"assistant", "lanlan", "neko"})


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一条规范化的对话轮次。"""

    conversation_id: str
    timestamp: float
    lanlan_name: str
    turn_type: str

    @property
    def is_user(self) -> bool:
        return self.turn_type.lower() in USER_TURN_TYPES


@dataclass(frozen=True, slots=True)
class BehaviorSummary:
    """一轮采样得到的增量事实（只描述"新出现的"轮次）。"""

    user_turns: int = 0
    assistant_turns: int = 0
    last_user_at: float | None = None
    first_user_at: float | None = None
    hour_histogram: tuple[int, ...] = (0,) * 24
    conversation_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_turns": self.user_turns,
            "assistant_turns": self.assistant_turns,
            "last_user_at": self.last_user_at,
            "first_user_at": self.first_user_at,
            "hour_histogram": list(self.hour_histogram),
            "conversation_ids": list(self.conversation_ids),
        }


def empty_summary() -> BehaviorSummary:
    return BehaviorSummary()


def _read_first(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            value = raw[key]
            if value is not None:
                return value
    return None


def normalize_turn(raw: Mapping[str, Any] | None, *, fallback_lanlan: str = "") -> TurnRecord | None:
    """把总线记录规范成 `TurnRecord`。

    角色名优先取记录自带的 `lanlan_name`，其次取 `metadata.lanlan_name`，最后用调用方给的兜底；
    轮次类型取 `metadata.turn_type`，缺失时按 "user" 之外处理（避免把助手轮次误算成互动）。
    无法判定 id/时间戳的记录直接丢弃。
    """
    if not isinstance(raw, Mapping):
        return None
    metadata = raw.get("metadata")
    meta: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}

    conversation_id = _read_first(raw, "conversation_id", "id", "key")
    timestamp = _read_first(raw, "timestamp", "time", "created_at")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None

    lanlan = _read_first(raw, "lanlan_name")
    if not isinstance(lanlan, str) or not lanlan.strip():
        meta_lanlan = _read_first(meta, "lanlan_name")
        lanlan = meta_lanlan if isinstance(meta_lanlan, str) and meta_lanlan.strip() else fallback_lanlan

    turn_type = _read_first(meta, "turn_type")
    if not isinstance(turn_type, str) or not turn_type.strip():
        raw_type = _read_first(raw, "turn_type")
        turn_type = raw_type if isinstance(raw_type, str) and raw_type.strip() else "unknown"

    return TurnRecord(
        conversation_id=conversation_id,
        timestamp=float(timestamp),
        lanlan_name=lanlan.strip(),
        turn_type=turn_type.strip(),
    )


def select_new_turns(
    records: Iterable[Mapping[str, Any]],
    *,
    lanlan: str,
    seen_ids: Iterable[str],
    fallback_lanlan: str = "",
) -> tuple[tuple[TurnRecord, ...], tuple[str, ...]]:
    """挑出属于 `lanlan` 且没见过的新轮次，返回 `(新轮次, 本轮见到的新 id)`。

    `lanlan` 为空表示"不限角色"（调用方明确允许跨角色聚合时才这么用）。
    已见 id 集合由调用方持久化，因此重启后不会把旧轮次重新算成新的互动。
    """
    seen = set(seen_ids)
    fresh: list[TurnRecord] = []
    fresh_ids: list[str] = []
    for raw in records:
        record = normalize_turn(raw, fallback_lanlan=fallback_lanlan)
        if record is None:
            continue
        if record.conversation_id in seen:
            continue
        if lanlan and record.lanlan_name and record.lanlan_name != lanlan:
            # 记录明确属于别的角色卡：跳过，但也不算"已见"（免得下次换角色时漏掉）
            continue
        seen.add(record.conversation_id)
        fresh_ids.append(record.conversation_id)
        fresh.append(record)
    fresh.sort(key=lambda item: item.timestamp)
    return tuple(fresh), tuple(fresh_ids)


def summarize_turns(turns: Iterable[TurnRecord]) -> BehaviorSummary:
    """聚合新轮次：主人发言数、最近/最早发言时间、24 小时时段分布。"""
    user_at: list[float] = []
    assistant_count = 0
    histogram = [0] * 24
    ids: list[str] = []
    for turn in turns:
        ids.append(turn.conversation_id)
        if turn.is_user:
            user_at.append(turn.timestamp)
            histogram[local_hour(turn.timestamp)] += 1
        elif turn.turn_type.lower() in ASSISTANT_TURN_TYPES:
            assistant_count += 1
    if not user_at:
        return BehaviorSummary(
            user_turns=0,
            assistant_turns=assistant_count,
            hour_histogram=tuple(histogram),
            conversation_ids=tuple(ids),
        )
    return BehaviorSummary(
        user_turns=len(user_at),
        assistant_turns=assistant_count,
        last_user_at=max(user_at),
        first_user_at=min(user_at),
        hour_histogram=tuple(histogram),
        conversation_ids=tuple(ids),
    )


def local_hour(timestamp: float) -> int:
    """本地时区小时数（0-23）。"""
    try:
        return datetime.fromtimestamp(float(timestamp)).hour % 24
    except (OverflowError, OSError, ValueError):
        return 0


def local_day(timestamp: float) -> str:
    """本地时区日期（YYYY-MM-DD），用于连续天数结算。"""
    try:
        return datetime.fromtimestamp(float(timestamp)).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def gap_hours(now: float, earlier: float | None) -> float:
    """距更早那个时刻过了多少小时；`earlier` 为空表示"从未"→ 返回 0（交由调用方另行处理）。"""
    if earlier is None:
        return 0.0
    return max(0.0, (float(now) - float(earlier)) / 3600.0)
