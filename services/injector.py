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
from ..core.events import (
    EVENT_CHEERED_UP,
    EVENT_SICK_RECOVERY,
    HEALTH_RECOVERY_LINE,
    MOOD_RECOVERY_LINE,
    StagedEvent,
)
from ..core.injection import (
    TRIGGER_ANNIVERSARY,
    TRIGGER_COMPANY,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_HUNGRY,
    TRIGGER_INTERVAL,
    TRIGGER_JOB,
    TRIGGER_JUDGMENT,
    TRIGGER_STAGED_EVENT,
    TRIGGER_TIER_CHANGE,
    TRIGGER_TIRED,
    build_text,
    resolve_ai_behavior,
)
from ..core.judgment import JUDGMENT_WEIGHTS
from ..core.model import (
    HEALTH_TIERS,
    MOOD_TIERS,
    STAT_NAMES,
    crisis_axes,
    tier_index_of,
    tier_of,
)
from ..core.rhythm import Anniversary, DailyRhythm
from .state import ShardState

__all__ = [
    "DRIFT_THRESHOLD",
    "InjectionPlan",
    "Injector",
    "recovery_line_for",
    "wake_suppressed",
]

# 「显著漂移」判据：任一轴距上次注入变了这么多分，才值得在非事件路径上打扰
DRIFT_THRESHOLD = 5.0
_HOUR = 3600.0

# 事件 → 它检出的那条线（只用于把跨越写成"从哪一档到哪一档"，线本身不进正文）。
_EVENT_RECOVERY_LINE: dict[str, float] = {
    EVENT_SICK_RECOVERY: HEALTH_RECOVERY_LINE,
    EVENT_CHEERED_UP: MOOD_RECOVERY_LINE,
}


def recovery_line_for(event: StagedEvent) -> float:
    """事件对应的「好起来了」那条线（见 `core/events` 的档位下界判据）。"""
    return _EVENT_RECOVERY_LINE.get(event.key, event.value - event.width)


def wake_suppressed(
    *,
    event: StagedEvent,
    settings: OurLifeSettings,
    rhythm: "DailyRhythm | None",
) -> bool:
    """睡眠静默：只对"好消息"生效（`wake_ok=False`），危机解除通知照发。

    这个判断与 `core/events` 的 `wake_ok` 是**同一条**语义的两处表达——
    那边定义"这件事该不该吵醒她"，这里执行它。凌晨三点把她叫醒说"我病好了"
    与说"我饿了"在体验上是两件不同的事：后者该被照顾，前者纯属打扰。
    """
    if not event.wake_ok and settings.inject.quiet_during_sleep:
        return rhythm is not None and rhythm.sleeping
    return False


def _suppressed_by_same_axis(
    *,
    state: ShardState,
    settings: OurLifeSettings,
    event: StagedEvent,
) -> bool:
    """她还在这个轴的危机档里就不发（见 `plan_for_event` 的同轴抑制说明）。

    只两条轴可能出现在事件里（病愈看健康、哄好看心情），所以这里只映射这两条——
    不做一个通用的"任意轴 → 危机阈值"表：那份通用表已经存在于
    `core/model.crisis_axes`，这里刻意只取**同一轴**的那一格，避免把语义扩大成
    "任何轴危机都抑制事件"。
    """
    configured = {
        "health": settings.inject.crisis_health_tier,
        "mood": settings.inject.crisis_mood_tier,
    }.get(event.stat)
    if configured is None:
        return False
    tiers = {"health": HEALTH_TIERS, "mood": MOOD_TIERS}.get(event.stat, ())
    if configured not in tiers:
        # 配置里写了不存在的档名：按"没有危机阈值"处理，而不是猜一个。
        # （`core/configuration` 不对 crisis_* 做白名单校验，所以这条是真实可能发生的输入。）
        return False
    return tier_index_of(event.stat, event.value) <= tiers.index(configured)


def _behavior_for_event(*, event: StagedEvent) -> str:
    """事件的开口档：病愈（`wake_ok=True`，危机解除通知）主动开口，哄好静默。

    判据刻意用 `wake_ok` 这个**语义字段**而不是 `key == EVENT_SICK_RECOVERY`：
    "这条该不该吵醒她"与"这条该不该主动开口"在本设计里是同一件事
    （都是"它是不是危机解除的通知"），复用同一个字段就不会出现
    "某个新事件该开口却没开口"的静默不一致。
    """
    return "respond" if event.wake_ok else "read"


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

    def plan_for_event(
        self,
        *,
        state: ShardState,
        settings: OurLifeSettings,
        now: float,
        event: StagedEvent,
        rhythm: "DailyRhythm | None" = None,
    ) -> InjectionPlan | None:
        """阶段性事件（v0.4.0）：病愈 / 哄好。

        **刻意做成与 `plan_for_tick` 并列的独立通道，而不是插进它的优先级链**，
        原因有两个（都属于"真实会错"的那种）：

        1. **不该被危机挤掉**：病愈那一拍通常整体状态已经在回升，但它可能恰好
           仍落在别的轴的危机档里（例如刚病好但饿着）。若把它塞进单条优先级链，
           会让"刚好了"被"还饿着"挤掉，两条都讲不成。
        2. **不该挤掉危机**：反过来也一样——饥/累危机是"现在就饿了"，
           不能被一件已经发生过的好事替代。
        两条各走各的，只共享 `max_per_hour` 这个总闸门。

        **同轴抑制**：她还在这个轴的危机档里就不发——"你终于好起来了"与
        下一行"你现在病着"是自相矛盾的。判据只取**同一条轴**的危机阈值
        （病愈看健康、哄好看心情），不牵连别的轴。

        睡眠：`wake_ok=False`（哄好）在睡觉时直接不发，也不记账——
        事件本身由 `state.note_event` 在调用侧**无条件**记下（她会醒来后
        在面板上看到这条经历），这里只决定"要不要说话"，见 `core/events` 第 4 条。
        """
        if not settings.enabled or not settings.events.enabled:
            return None
        if wake_suppressed(event=event, settings=settings, rhythm=rhythm):
            return None
        inject = settings.inject
        if len(_prune_window(state.inject_timestamps, now)) >= inject.max_per_hour:
            return None
        if _suppressed_by_same_axis(state=state, settings=settings, event=event):
            return None

        text = build_text(
            stats=state.stats,
            trigger=TRIGGER_STAGED_EVENT,
            streak_days=state.streak_days,
            gap_hours=None if state.last_touch_at is None else max(0.0, (now - state.last_touch_at) / _HOUR),
            transitions=(
                (event.stat, tier_of(event.stat, recovery_line_for(event)), tier_of(event.stat, event.value)),
            ),
            max_chars=inject.max_chars,
            rhythm=rhythm,
            day_number=state.day_number,
            staged_event=event,
        )
        behavior = _behavior_for_event(event=event)
        if behavior == "respond" and len(_prune_window(state.respond_timestamps, now)) >= inject.respond_max_per_hour:
            # 主动开口额度用完：降级为静默注入，而不是整条丢掉（与 plan_for_tick 同一条纪律）
            behavior = "read"
        return InjectionPlan(text=text, trigger=TRIGGER_STAGED_EVENT, ai_behavior=behavior, lanlan=state.lanlan)

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

    def plan_for_job(
        self,
        *,
        state: ShardState,
        settings: OurLifeSettings,
        now: float,
        job_line: str,
        rhythm: "DailyRhythm | None" = None,
    ) -> InjectionPlan | None:
        """打工下班叙事（v0.7.0）：与 `plan_for_event` 并列的独立通道。

        为什么不走 `plan_for_tick` 的优先级链：下班是一条**已经发生过的事实**，
        和"她此刻状态如何"同拍抵达时，两者都想说——单链会互相顶掉（阶段事件
        同样理由，见其 docstring）。只共享 `max_per_hour` 总闸门。

        永远 `read`：钱已经挣回来了，不是危机通知，不该打断主人；
        但她要知道自己今天上过班、带回了多少（金币是"今天的 factual 事件"，
        不在"不给模型看数字"的五轴禁令里）。

        睡眠：结算照常发生（在调用侧，钱照发、损耗照扣），这里只决定要不要
        把这句话送进上下文——她累到直接睡着时不该还在"播报下班"。
        """
        if not settings.enabled or not settings.job.enabled:
            return None
        inject = settings.inject
        if len(_prune_window(state.inject_timestamps, now)) >= inject.max_per_hour:
            return None
        if inject.quiet_during_sleep and rhythm is not None and rhythm.sleeping:
            return None
        text = build_text(
            stats=state.stats,
            trigger=TRIGGER_JOB,
            streak_days=state.streak_days,
            gap_hours=None if state.last_touch_at is None else max(0.0, (now - state.last_touch_at) / _HOUR),
            max_chars=inject.max_chars,
            rhythm=rhythm,
            day_number=state.day_number,
            job_line=job_line,
        )
        return InjectionPlan(text=text, trigger=TRIGGER_JOB, ai_behavior="read", lanlan=state.lanlan)

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
