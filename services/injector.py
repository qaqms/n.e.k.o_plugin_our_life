"""注入决策与投递：事件驱动 + 频控 + 作息抑制。

触发优先级（v0.2.0 起，按"她的需求有多紧要"排序）：

1. **饿坏了**：饱食掉进危机档（`starving`）→ 她可以主动开口催饭。
2. **累垮了**：精力掉进危机档（`exhausted`）→ 同上。
3. **危机**：心情 / 身体掉进危机档 → 立即可注入，并按 `respond_on_crisis` 升级为主动开口。
4. **纪念日**：相处天数命中锚点（`core/rhythm.ANNIVERSARY_DAYS`）。
5. **跨档**：任一轴档位变化（带 0.05 分迟滞，见 `core/model.TIER_CROSSING_MARGIN`）。
6. **跨天首触**：今天第一次来找她。
7. **显著漂移**：有新的互动、距上次注入已过 `min_interval_sec`、且任一轴变化 ≥ 5 分。

三层频控：

- `inject.max_per_hour`：所有注入的小时上限（**危机与饥/累危机档绕过 `min_interval`，但不绕过它**）。
- `inject.respond_max_per_hour`：`respond`（让她主动开口）单独的小时上限，默认 1。
  v0.1.0 只有总上限，真机上"跨档 + 危机"叠加会连着弹好几次主动开口，所以独立限流。
- `inject.quiet_during_sleep`：她在睡觉时抑制**非危机**注入——凌晨三点把她叫醒说"我饿了"
  与插件想营造的"过日子"感完全相反；危机（真生病/饿坏了）仍然允许，
  因为那时候她本来就该被照顾。

投递契约：

- `push_message` 的 `visibility=[]`（用户不直接看到这段文本）+ `ai_behavior` 决定模型怎么处理。
- `coalesce_key`：同一条触发理由在宿主侧折叠成最新一条，避免她连发时说同一件事。
- `submitted=True` **只代表本地提交成功**，不代表宿主已消费；因此上层只把它当"未被我方拒绝"。
- 注入正文里带 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符，由宿主在注入边界展开。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..core.configuration import OurLifeSettings
from ..core.injection import (
    TRIGGER_ANNIVERSARY,
    TRIGGER_COMPANY,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_HUNGRY,
    TRIGGER_INTERVAL,
    TRIGGER_JUDGMENT,
    TRIGGER_TIER_CHANGE,
    TRIGGER_TIRED,
    build_text,
    resolve_ai_behavior,
)
from ..core.judgment import JUDGMENT_WEIGHTS
from ..core.model import STAT_NAMES, crisis_axes
from ..core.rhythm import Anniversary, DailyRhythm
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

    @property
    def wants_reply(self) -> bool:
        """这条是否要让宿主起新一轮（= 主动开口）。"""
        return self.ai_behavior == "respond"


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
        rhythm: "DailyRhythm | None" = None,
        anniversary: "Anniversary | None" = None,
        anniversary_seen: bool = False,
        judgment_label: str = "",
    ) -> InjectionPlan | None:
        """按优先级挑一个注入理由；没有理由就返回 None（绝大多数 tick 都是 None）。"""
        if not settings.enabled:
            return None
        inject = settings.inject
        if len(_prune_window(state.inject_timestamps, now)) >= inject.max_per_hour:
            return None
        if anniversary_seen:
            # 纪念日这一档今天已经注入过，不再重复
            anniversary = None

        axes = crisis_axes(state.stats, inject)
        crisis = bool(axes)
        starving = "satiety" in axes
        exhausted = "energy" in axes
        within_interval = state.last_inject_at is not None and (now - state.last_inject_at) < inject.min_interval_sec
        trigger: str | None = None
        if starving:
            trigger = TRIGGER_HUNGRY
        elif exhausted:
            trigger = TRIGGER_TIRED
        elif crisis and (state.last_inject_at is None or transitions or had_new_interaction):
            trigger = TRIGGER_CRISIS
        elif anniversary is not None:
            trigger = TRIGGER_ANNIVERSARY
        elif transitions:
            trigger = TRIGGER_TIER_CHANGE
        elif is_new_day and had_new_interaction:
            trigger = TRIGGER_DAILY_GREET
        elif JUDGMENT_WEIGHTS.get(judgment_label, 0.0) != 0.0:
            # 反馈闭环：她**真的**给出了带方向的判断时，才让这个感受进上下文。
            # `neutral`（含一切被收敛掉的非法标签）权重为 0 —— 那种情况什么都不说，
            # 免得"她回味了一下但没什么感觉"变成一句空话挤占 prompt。
            trigger = TRIGGER_JUDGMENT

        if trigger is None:
            if within_interval or not had_new_interaction:
                return None
            if not _drifted_since_last_injection(state):
                return None
            trigger = TRIGGER_INTERVAL

        if self._quieted(trigger=trigger, settings=settings, rhythm=rhythm, crisis=crisis):
            return None

        behavior = resolve_ai_behavior(trigger, state.stats, inject)
        if behavior == "respond" and len(_prune_window(state.respond_timestamps, now)) >= inject.respond_max_per_hour:
            # 主动开口的额度用完了：降级为静默注入，而不是整条丢掉
            # （状态本身仍有信息量，只是不许打断他）
            behavior = "read"

        text = build_text(
            stats=state.stats,
            trigger=trigger,
            streak_days=state.streak_days,
            gap_hours=None if state.last_touch_at is None else max(0.0, (now - state.last_touch_at) / _HOUR),
            transitions=transitions,
            max_chars=inject.max_chars,
            rhythm=rhythm,
            anniversary=anniversary,
            day_number=state.day_number,
            judgment_label=judgment_label if trigger == TRIGGER_JUDGMENT else "",
        )
        return InjectionPlan(text=text, trigger=trigger, ai_behavior=behavior, lanlan=state.lanlan)

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
            day_number=state.day_number,
        )
        return InjectionPlan(text=text, trigger=TRIGGER_COMPANY, ai_behavior="respond", lanlan=state.lanlan)

    def _quieted(
        self,
        *,
        trigger: str,
        settings: OurLifeSettings,
        rhythm: "DailyRhythm | None",
        crisis: bool,
    ) -> bool:
        """她在睡觉时压住非危机注入（见模块 docstring 的第三层频控）。"""
        if not settings.inject.quiet_during_sleep:
            return False
        if rhythm is None or not rhythm.sleeping:
            return False
        if trigger in (TRIGGER_COMPANY, TRIGGER_ANNIVERSARY):
            return False
        return not crisis

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
            "priority": _priority_for(plan.trigger),
            # 同一条理由折叠成最新一条：她连发时不会把同一句话叠好几遍
            "coalesce_key": f"our_life:{plan.trigger}",
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


def _priority_for(trigger: str) -> int:
    """消息优先级（0-10，见 SDK 文档）；危机类让她更容易被听见。"""
    if trigger in (TRIGGER_CRISIS, TRIGGER_HUNGRY, TRIGGER_TIRED, TRIGGER_COMPANY):
        return 6
    if trigger == TRIGGER_ANNIVERSARY:
        return 5
    return 3


def _prune_window(stamps: tuple[float, ...], now: float) -> tuple[float, ...]:
    return tuple(stamp for stamp in stamps if stamp > now - _HOUR)


def _drifted_since_last_injection(state: ShardState) -> bool:
    """相对"上次注入时的数值"漂移是否够大（没有注入历史则不算漂移）。

    比较对象刻意是**上次注入时**的快照，而不是上一拍的快照：否则每次衰减一丁点就累积成
    "变化很大"，会把 min_interval 频控架空。

    比较**全部五轴**：v0.1.0 只比好感/心情/健康，v0.2.0 加了饱食与精力却漏在这里——
    结果"她饿到掉档但心情没动"这种最该说话的情形反而不触发漂移注入。
    """
    before = state.last_injected_stats()
    if before is None:
        return False
    deltas = tuple(
        abs(getattr(state.stats, name) - getattr(before, name)) for name in STAT_NAMES
    )
    return max(deltas) >= DRIFT_THRESHOLD
