"""注入决策与投递：事件驱动 + 三重频控。

触发优先级（见 DESIGN.md）：
1. **危机**：心情/健康掉进危机档 → 立即可注入（绕过 `min_interval_sec`，但受小时上限约束），
   并按 `respond_on_crisis` 升级为 `ai_behavior="respond"` 让她主动开口。
2. **跨档**：任一轴档位变化 → 立即可注入（同样绕过 min_interval，受小时上限约束）。
3. **跨天首触**：今天第一次来找她 → 注入一次"今天的状态"。
4. **显著漂移**：有新的互动、距上次注入已过 `min_interval_sec`、且任一轴变化 ≥ 5 分。

投递契约：
- `push_message` 的 `visibility=[]`（用户不直接看到这段文本）+ `ai_behavior` 决定模型怎么处理。
- `submitted=True` **只代表本地提交成功**，不代表宿主已消费；因此上层只把它当"未被我方拒绝"。
- 注入正文里带 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符，由宿主在注入边界展开。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..core.configuration import OurLifeSettings
from ..core.injection import (
    TRIGGER_COMPANY,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_INTERVAL,
    TRIGGER_TIER_CHANGE,
    build_text,
    resolve_ai_behavior,
)
from ..core.model import is_crisis
from .state import ShardState

__all__ = ["DRIFT_THRESHOLD", "InjectionPlan", "Injector"]

# 「显著漂移」判据：任一轴距上次注入变了这么多分，才值得在非事件路径上打扰
DRIFT_THRESHOLD = 5.0
_HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    text: str
    trigger: str
    ai_behavior: str
    lanlan: str


class Injector:
    def __init__(self, plugin: Any, *, logger: Any = None):
        self._plugin = plugin
        self._logger = logger

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    def plan_for_tick(
        self,
        *,
        state: ShardState,
        settings: OurLifeSettings,
        now: float,
        transitions: tuple[tuple[str, str, str], ...],
        had_new_interaction: bool,
        is_new_day: bool,
    ) -> InjectionPlan | None:
        """按优先级挑一个注入理由；没有理由就返回 None（绝大多数 tick 都是 None）。"""
        if not settings.enabled:
            return None
        inject = settings.inject
        stamps = _prune_window(state.inject_timestamps, now)
        if len(stamps) >= inject.max_per_hour:
            return None

        crisis = is_crisis(state.stats, inject)
        within_interval = state.last_inject_at is not None and (now - state.last_inject_at) < inject.min_interval_sec

        trigger: str | None = None
        if crisis and (state.last_inject_at is None or transitions or had_new_interaction):
            trigger = TRIGGER_CRISIS
        elif transitions:
            trigger = TRIGGER_TIER_CHANGE
        elif is_new_day and had_new_interaction:
            trigger = TRIGGER_DAILY_GREET

        if trigger is None:
            if within_interval or not had_new_interaction:
                return None
            if not _drifted_since_last_injection(state):
                return None
            trigger = TRIGGER_INTERVAL

        text = build_text(
            stats=state.stats,
            trigger=trigger,
            streak_days=state.streak_days,
            gap_hours=None if state.last_touch_at is None else max(0.0, (now - state.last_touch_at) / _HOUR),
            transitions=transitions,
            max_chars=inject.max_chars,
        )
        return InjectionPlan(
            text=text,
            trigger=trigger,
            ai_behavior=resolve_ai_behavior(trigger, state.stats, inject),
            lanlan=state.lanlan,
        )

    def plan_for_company(self, *, state: ShardState, settings: OurLifeSettings, now: float) -> InjectionPlan | None:
        """LLM 工具「索取陪伴」：想让她主动撒娇时走这条路（带冷却）。"""
        if not settings.enabled:
            return None
        if state.company_last_at is not None and (now - state.company_last_at) < settings.inject.company_cooldown_sec:
            return None
        text = build_text(
            stats=state.stats,
            trigger=TRIGGER_COMPANY,
            streak_days=state.streak_days,
            gap_hours=None if state.last_touch_at is None else max(0.0, (now - state.last_touch_at) / _HOUR),
            max_chars=settings.inject.max_chars,
        )
        return InjectionPlan(text=text, trigger=TRIGGER_COMPANY, ai_behavior="respond", lanlan=state.lanlan)

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------

    async def emit(self, plan: InjectionPlan) -> bool:
        """投递注入。返回 `submitted`（**不是**"已送达"）。"""
        payload: dict[str, Any] = {
            "visibility": [],
            "ai_behavior": plan.ai_behavior,
            "parts": [{"type": "text", "text": plan.text}],
            "source": "our_life",
            "metadata": {"trigger": plan.trigger},
            "priority": 3,
        }
        if plan.lanlan:
            payload["target_lanlan"] = plan.lanlan
        result = await self._send(payload)
        submitted = bool(isinstance(result, dict) and result.get("submitted"))
        if not submitted:
            reason = result.get("reason") if isinstance(result, dict) else None
            self._log(f"inject not submitted (trigger={plan.trigger}, reason={reason})")
        return submitted

    async def _send(self, payload: dict[str, Any]) -> Any:
        ctx = getattr(self._plugin, "ctx", None)
        async_sender = getattr(ctx, "push_message_async", None) or getattr(
            self._plugin, "push_message_async", None
        )
        if callable(async_sender):
            try:
                return await async_sender(**payload)
            except Exception:
                self._log("push_message_async failed", exc=True)
        sync_sender = getattr(self._plugin, "push_message", None) or getattr(ctx, "push_message", None)
        if not callable(sync_sender):
            return None
        try:
            # 同步接口：`push_message` 是同步方法，放进线程池，避免阻塞事件循环
            return await asyncio.to_thread(sync_sender, **payload)
        except Exception:
            self._log("push_message failed", exc=True)
            return None

    def _log(self, message: str, *, exc: bool = False) -> None:
        if self._logger is None:
            return
        try:
            if exc:
                self._logger.warning(message, exc_info=True)
            else:
                self._logger.info(message)
        except Exception:
            pass


def _prune_window(stamps: tuple[float, ...], now: float) -> tuple[float, ...]:
    return tuple(stamp for stamp in stamps if stamp > now - _HOUR)


def _drifted_since_last_injection(state: ShardState) -> bool:
    """相对"上次注入时的数值"漂移是否够大（没有注入历史则不算漂移）。

    比较对象刻意是**上次注入时**的快照，而不是上一拍的快照：否则每次衰减一丁点就累积成
    "变化很大"，会把 min_interval 频控架空。
    """
    before = state.last_injected_stats()
    if before is None:
        return False
    deltas = (
        abs(state.stats.affection - before.affection),
        abs(state.stats.mood - before.mood),
        abs(state.stats.health - before.health),
    )
    return max(deltas) >= DRIFT_THRESHOLD
