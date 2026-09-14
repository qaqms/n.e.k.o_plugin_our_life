"""按角色卡分片的数值状态：内存工作副本 + PluginStore 持久化。

设计取舍：
- store 通道故障（或 `[plugin.store].enabled` 被手改成 false 导致静默失效）时，
  插件**仍然工作**，只是状态活不过进程重启。因此这里始终保留内存缓存，
  持久化失败只降级、不抛给调用方。
- 每个角色一个 key：`ourlife@<lanlan_name>`。键名沿用 forever_companion 的 `mood@<角色>` 风格。
- `seen_conversation_ids` 负责重启后的去重：不存它的话，重启会把最近几十条旧轮次
  重新算成"新互动"，凭空刷一波数值。
- 该存的东西一律 JSON 安全（dict/list/str/num/bool/None），因为 store 会直接序列化。

**schema 版本与向后兼容**（v0.2.0 起）：

- `_SCHEMA_VERSION = 2`：新增 `satiety` / `energy` 两轴、金币、背包、吃饭账、纪念日、
  首次相处日期、sleeping 快照位。
- 旧分片（v1）**不需要迁移脚本**：`ShardState.from_payload` 对每个字段单独回退默认值，
  缺 `satiety`/`energy` 时会补上 `Stats()` 的默认值，金币补 `start_sodas`。
  这是刻意设计——真机上跑着 v0.1.0 的分片，升级后必须能直接读。
- **不写入** `schema_version` 之外的"迁移标记"：一旦要写迁移逻辑，就在 `from_payload`
  里按 `_SCHEMA_VERSION` 分支，而不是让文件自己带状态机。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from ..core.behavior import BehaviorSummary
from ..core.economy import Inventory, observed_meals_per_day
from ..core.model import STAT_NAMES, Stats
from ..core.rhythm import day_number_of

__all__ = [
    "KEY_PREFIX",
    "MEAL_DAYS_KEEP",
    "SEEN_IDS_MAX",
    "ShardState",
    "StateStore",
    "day_number_for",
    "lanlan_from_key",
    "shard_key",
]

KEY_PREFIX = "ourlife@"
SEEN_IDS_MAX = 512
INJECT_HISTORY_MAX = 20
MEAL_DAYS_KEEP = 14
_SCHEMA_VERSION = 2


def shard_key(lanlan: str) -> str:
    return f"{KEY_PREFIX}{lanlan}"


def lanlan_from_key(key: str) -> str:
    return key[len(KEY_PREFIX) :] if key.startswith(KEY_PREFIX) else ""


@dataclass(slots=True)
class ShardState:
    """一个角色卡的运行中状态（内存工作副本）。"""

    lanlan: str
    stats: Stats = Stats()
    # 最近一次「折算落盘」的时刻：惰性衰减的基准点
    last_decay_at: float = 0.0
    # 最近一次主人发言（权威"来过"时间）
    last_touch_at: float | None = None
    streak_days: int = 0
    last_active_date: str = ""
    # 相遇日期（相处第几天 / 纪念日都按它算，断档也不会错位）
    first_day: str = ""
    day_number_seen: int = 0
    milestones: tuple[int, ...] = ()
    # 已经结算过的冷落天数（按增量扣，保证幂等）
    neglect_days_applied: float = 0.0
    # 同会话递减收益用的会话标识与计数
    session_key: str = ""
    session_turns: int = 0
    # 注入频控
    last_inject_at: float | None = None
    inject_timestamps: tuple[float, ...] = ()
    inject_history: tuple[dict[str, Any], ...] = ()
    # 让她主动开口（respond）的独立频控窗口
    respond_timestamps: tuple[float, ...] = ()
    # 「索取陪伴」冷却
    company_last_at: float | None = None
    # 已消费的对话轮次 id（重启后去重）
    seen_conversation_ids: tuple[str, ...] = ()
    # 互动时段分布（本地小时 → 累计次数），用于粗略作息画像
    hour_histogram: tuple[int, ...] = (0,) * 24
    # ------------------------------------------------------------------
    # v0.2.0 经济层
    # ------------------------------------------------------------------
    sodas: int = 0
    inventory: Inventory = Inventory()
    # 当日账：`account_date` 一变就是新的一天（日薪资格与消费上限都按它重置）
    account_date: str = ""
    daily_allowance_granted: bool = False
    daily_spent: int = 0
    # (日期, 当日餐数)，只留最近两周——面板的"实测每天吃几餐"用它
    meal_days: tuple[tuple[str, int], ...] = ()
    meals_total: int = 0
    last_meal_at: float | None = None
    # 她最近一次是"睡着"状态（进食判据在睡眠期放宽到"醒来吃"）
    sleeping: bool = False
    updated_at: float = 0.0
    # 上一拍的档位快照（用于跨拍判跨档；不持久化）
    tier_snapshot: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 派生读数
    # ------------------------------------------------------------------

    @property
    def day_number(self) -> int:
        """相处第几天（相遇当天 = 1）；没有相遇日期时返回 0。"""
        return self.day_number_seen

    def meals_per_day(self, *, window_days: int = 7) -> float:
        """最近实测的每日餐数（没有记录返回 0，由口粮顾问用理论值兜底）。"""
        return observed_meals_per_day(self.meal_days, window_days=window_days)

    def inventory_counts(self) -> dict[str, int]:
        return self.inventory.as_dict()

    # ------------------------------------------------------------------
    # 变更
    # ------------------------------------------------------------------

    def today_meal_count(self, day: str) -> int:
        for entry_day, count in self.meal_days:
            if entry_day == day:
                return int(count)
        return 0

    def apply_summary(self, summary: BehaviorSummary) -> None:
        """把一轮采样的增量事实吸收进状态（只更新"事实"，数值变化由 model 负责）。"""
        if summary.user_turns <= 0:
            self.remember_turns(summary.conversation_ids)
            return
        self.session_turns += summary.user_turns
        if summary.last_user_at is not None:
            self.last_touch_at = summary.last_user_at
        self.merge_hour_histogram(summary.hour_histogram)
        self.remember_turns(summary.conversation_ids)

    def remember_turns(self, conversation_ids: Iterable[str]) -> None:
        """记下已消费的轮次 id，保持有界（只留最近 N 个）。"""
        merged = list(self.seen_conversation_ids)
        for conversation_id in conversation_ids:
            if conversation_id and conversation_id not in merged:
                merged.append(conversation_id)
        self.seen_conversation_ids = tuple(merged[-SEEN_IDS_MAX:])

    def merge_hour_histogram(self, histogram: Iterable[int]) -> None:
        current = list(self.hour_histogram) or [0] * 24
        if len(current) != 24:
            current = [0] * 24
        for index, count in enumerate(histogram):
            if 0 <= index < 24:
                current[index] += max(0, _as_int(count, 0))
        self.hour_histogram = tuple(current)

    def note_injection(self, *, at: float, trigger: str, summary: str, stats: Stats | None = None) -> None:
        """记录一次注入（用于频控窗口、面板"近期注入"列表，以及"距上次注入漂移了多少"）。"""
        self.last_inject_at = at
        stamps = [stamp for stamp in (*self.inject_timestamps, at) if stamp > at - 7200.0]
        self.inject_timestamps = tuple(sorted(stamps))
        entry: dict[str, Any] = {"at": at, "trigger": trigger, "summary": summary[:120]}
        if stats is not None:
            # 存一份数值快照，「显著漂移」判据就能比较"相对上次注入"的变化
            entry["stats"] = stats.as_dict()
        history = [*self.inject_history, entry]
        self.inject_history = tuple(history[-INJECT_HISTORY_MAX:])

    def note_respond(self, *, at: float) -> None:
        """记一次"让她主动开口"，用于 respond 的独立小时上限。"""
        stamps = [stamp for stamp in (*self.respond_timestamps, at) if stamp > at - 7200.0]
        self.respond_timestamps = tuple(sorted(stamps))

    def last_injected_stats(self) -> Stats | None:
        for entry in reversed(self.inject_history):
            raw = entry.get("stats")
            if isinstance(raw, Mapping):
                return Stats.from_mapping(raw)
        return None

    def with_stats(self, stats: Stats) -> "ShardState":
        return replace(self, stats=stats)

    def snapshot_for_panel(self, *, now: float) -> dict[str, Any]:
        """面板用的只读快照（含档位，不含任何用户原文）。"""
        from ..core.model import tier_of

        return {
            "lanlan": self.lanlan,
            "affection": round(self.stats.affection, 2),
            "mood": round(self.stats.mood, 2),
            "health": round(self.stats.health, 2),
            "satiety": round(self.stats.satiety, 2),
            "energy": round(self.stats.energy, 2),
            "tiers": {name: tier_of(name, getattr(self.stats, name)) for name in STAT_NAMES},
            "streak_days": self.streak_days,
            "day_number": self.day_number,
            "last_touch_at": self.last_touch_at,
            "gap_hours": None if self.last_touch_at is None else round(max(0.0, (now - self.last_touch_at) / 3600.0), 2),
            "last_inject_at": self.last_inject_at,
            "inject_count_24h": len([stamp for stamp in self.inject_timestamps if stamp > now - 86400.0]),
            "sodas": self.sodas,
            "daily_spent": self.daily_spent,
            "meals_total": self.meals_total,
            "meals_today": self.today_meal_count(self.account_date),
            "last_meal_at": self.last_meal_at,
            "sleeping": self.sleeping,
            "inventory": self.inventory_counts(),
            "updated_at": self.updated_at,
        }

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def as_payload(self) -> dict[str, Any]:
        """序列化成 store 可存的对象。"""
        return {
            "schema_version": _SCHEMA_VERSION,
            "stats": self.stats.as_dict(),
            "last_decay_at": self.last_decay_at,
            "last_touch_at": self.last_touch_at,
            "streak_days": self.streak_days,
            "last_active_date": self.last_active_date,
            "first_day": self.first_day,
            "day_number_seen": self.day_number_seen,
            "milestones": list(self.milestones),
            "neglect_days_applied": self.neglect_days_applied,
            "session_key": self.session_key,
            "session_turns": self.session_turns,
            "last_inject_at": self.last_inject_at,
            "inject_timestamps": list(self.inject_timestamps),
            "inject_history": list(self.inject_history),
            "respond_timestamps": list(self.respond_timestamps),
            "company_last_at": self.company_last_at,
            "seen_conversation_ids": list(self.seen_conversation_ids),
            "hour_histogram": list(self.hour_histogram),
            "sodas": self.sodas,
            "inventory": self.inventory_counts(),
            "account_date": self.account_date,
            "daily_allowance_granted": self.daily_allowance_granted,
            "daily_spent": self.daily_spent,
            "meal_days": [[day, count] for day, count in self.meal_days],
            "meals_total": self.meals_total,
            "last_meal_at": self.last_meal_at,
            "sleeping": self.sleeping,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_payload(
        cls,
        lanlan: str,
        payload: Mapping[str, Any] | None,
        *,
        now: float,
        default_stats: Stats | None = None,
        default_sodas: int = 0,
    ) -> "ShardState":
        """从持久化数据恢复；任何损坏字段单独回退，不让整份状态报废。

        缺键回退也是**旧分片（v1）的兼容路径**：v1 没有饱食/精力/金币/背包，
        读进来会补成默认值而不是报错（见模块 docstring 的 schema 一节）。
        """
        base = cls(
            lanlan=lanlan,
            stats=default_stats or Stats(),
            last_decay_at=now,
            updated_at=now,
            sodas=max(0, int(default_sodas)),
        )
        if not isinstance(payload, Mapping):
            return base

        raw_stats = payload.get("stats")
        stats = Stats.from_mapping(raw_stats if isinstance(raw_stats, Mapping) else None)

        return cls(
            lanlan=lanlan,
            stats=stats,
            last_decay_at=_as_float(payload.get("last_decay_at"), now),
            last_touch_at=_as_optional_float(payload.get("last_touch_at")),
            streak_days=max(0, _as_int(payload.get("streak_days"), 0)),
            last_active_date=_as_str(payload.get("last_active_date")),
            first_day=_as_str(payload.get("first_day")),
            day_number_seen=max(0, _as_int(payload.get("day_number_seen"), 0)),
            milestones=tuple(sorted({_as_int(x, -1) for x in _as_list(payload.get("milestones"))} - {-1})),
            neglect_days_applied=max(0.0, _as_float(payload.get("neglect_days_applied"), 0.0)),
            session_key=_as_str(payload.get("session_key")),
            session_turns=max(0, _as_int(payload.get("session_turns"), 0)),
            last_inject_at=_as_optional_float(payload.get("last_inject_at")),
            inject_timestamps=tuple(
                sorted(_as_float(x, 0.0) for x in _as_list(payload.get("inject_timestamps")) if _as_float(x, 0.0) > 0)
            ),
            inject_history=tuple(
                dict(item) for item in _as_list(payload.get("inject_history")) if isinstance(item, Mapping)
            ),
            respond_timestamps=tuple(
                sorted(_as_float(x, 0.0) for x in _as_list(payload.get("respond_timestamps")) if _as_float(x, 0.0) > 0)
            ),
            company_last_at=_as_optional_float(payload.get("company_last_at")),
            seen_conversation_ids=tuple(
                str(x) for x in _as_list(payload.get("seen_conversation_ids")) if isinstance(x, str) and x
            )[-SEEN_IDS_MAX:],
            hour_histogram=_as_histogram(payload.get("hour_histogram")),
            sodas=max(0, _as_int(payload.get("sodas"), max(0, int(default_sodas)))),
            inventory=Inventory.from_mapping(
                payload.get("inventory") if isinstance(payload.get("inventory"), Mapping) else None
            ),
            account_date=_as_str(payload.get("account_date")),
            daily_allowance_granted=_as_bool(payload.get("daily_allowance_granted")),
            daily_spent=max(0, _as_int(payload.get("daily_spent"), 0)),
            meal_days=_as_meal_days(payload.get("meal_days")),
            meals_total=max(0, _as_int(payload.get("meals_total"), 0)),
            last_meal_at=_as_optional_float(payload.get("last_meal_at")),
            sleeping=_as_bool(payload.get("sleeping")),
            updated_at=_as_float(payload.get("updated_at"), now),
        )


class StateStore:
    """PluginStore 的薄封装：写失败只降级，不抛出。"""

    def __init__(self, plugin: Any, *, logger: Any = None):
        self._plugin = plugin
        self._logger = logger
        self._cache: dict[str, ShardState] = {}

    @property
    def cached(self) -> dict[str, ShardState]:
        return self._cache

    @property
    def store_available(self) -> bool:
        """宿主**是否给了 store 通道对象**（唯一的"持久化到底可不可用"判据）。

        为什么不是"读写成不成功"：本模块的既定契约是**持久化失败只降级、不抛给调用方**
        （见模块 docstring），所以 `get`/`set` 返回 `Err` 时插件照常工作、只是活不过重启。
        那种失败是每拍都可能发生的瞬时故障，把它当成"存储不可用"会让面板一直报错，
        而真实原因是通道还在、只是这一下没通。

        通道对象**整个缺席**（`None`）则不同：那是持久化从头到尾不可能可用，属配置/装配错误，
        值得如实告诉用户。宿主侧 `router.store` 就是 `getattr(main_plugin, "store", None)`，
        所以这个状态是真实可达的，不是防御性摆设。
        """
        return getattr(self._plugin, "store", None) is not None

    def known_lanlans(self) -> tuple[str, ...]:
        return tuple(sorted(self._cache))

    async def list_persisted_lanlans(self) -> tuple[str, ...]:
        """从 store 里扫出所有分片键（用于重启后恢复全部分片）。"""
        store = getattr(self._plugin, "store", None)
        if store is None:
            return ()
        try:
            from plugin.sdk.plugin import unwrap_or

            keys = unwrap_or(await store.keys(prefix=KEY_PREFIX), [])
        except Exception:
            self._log_debug("list_persisted_lanlans failed", exc=True)
            return ()
        if not isinstance(keys, (list, tuple)):
            return ()
        return tuple(sorted({lanlan_from_key(k) for k in keys if isinstance(k, str) and lanlan_from_key(k)}))

    async def load(
        self,
        lanlan: str,
        *,
        now: float,
        default_stats: Stats | None = None,
        default_sodas: int = 0,
    ) -> ShardState:
        if not lanlan:
            raise ValueError("lanlan must be non-empty")
        cached = self._cache.get(lanlan)
        if cached is not None:
            return cached
        payload = await self._read_payload(lanlan)
        state = ShardState.from_payload(
            lanlan, payload, now=now, default_stats=default_stats, default_sodas=default_sodas
        )
        self._cache[lanlan] = state
        return state

    async def save(self, state: ShardState, *, now: float) -> bool:
        state.updated_at = now
        self._cache[state.lanlan] = state
        store = getattr(self._plugin, "store", None)
        if store is None:
            return False
        try:
            result = await store.set(shard_key(state.lanlan), state.as_payload())
        except Exception:
            self._log_debug("store.set raised", exc=True)
            return False
        return _result_ok(result)

    async def drop(self, lanlan: str) -> bool:
        self._cache.pop(lanlan, None)
        store = getattr(self._plugin, "store", None)
        if store is None:
            return False
        try:
            return _result_ok(await store.delete(shard_key(lanlan)))
        except Exception:
            self._log_debug("store.delete raised", exc=True)
            return False

    async def _read_payload(self, lanlan: str) -> Mapping[str, Any] | None:
        store = getattr(self._plugin, "store", None)
        if store is None:
            return None
        try:
            from plugin.sdk.plugin import unwrap_or

            payload = unwrap_or(await store.get(shard_key(lanlan), None), None)
        except Exception:
            self._log_debug("store.get raised", exc=True)
            return None
        return payload if isinstance(payload, Mapping) else None

    def _log_debug(self, message: str, *, exc: bool = False) -> None:
        if self._logger is None:
            return
        try:
            if exc:
                self._logger.debug(message, exc_info=True)
            else:
                self._logger.debug(message)
        except Exception:
            pass


def day_number_for(state: ShardState, today: str) -> int:
    """相处天数（相遇当天 = 1）；`first_day` 缺失时退回到 streak（v1 旧分片没有它）。"""
    number = day_number_of(state.first_day, today)
    if number > 0:
        return number
    return max(1, state.streak_days)


def _result_ok(result: Any) -> bool:
    """store 是 Result 式 API：取不到 is_ok 时按"有没有 error 字段"保守判断。"""
    checker = getattr(result, "is_ok", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return not (isinstance(result, Mapping) and result.get("error"))


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _as_histogram(value: Any) -> tuple[int, ...]:
    items = _as_list(value)
    if len(items) != 24:
        return (0,) * 24
    return tuple(max(0, _as_int(item, 0)) for item in items)


def _as_meal_days(value: Any) -> tuple[tuple[str, int], ...]:
    """吃饭账：只接受 `[日期, 数量]` 形状，保持有界且按日期升序。"""
    out: dict[str, int] = {}
    for entry in _as_list(value):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            day = _as_str(entry[0])
            count = _as_int(entry[1], 0)
        elif isinstance(entry, Mapping):
            day = _as_str(entry.get("day"))
            count = _as_int(entry.get("count"), 0)
        else:
            continue
        if day and count > 0:
            out[day] = out.get(day, 0) + count
    return tuple(sorted(out.items())[-MEAL_DAYS_KEEP:])


def now_seconds() -> float:
    return time.time()
