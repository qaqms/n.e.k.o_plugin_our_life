"""我们的生活 (our_life) —— 养成系陪伴插件。

架构（一段话）：`core/` 是零 SDK 依赖的纯函数层（数值模型 / 惰性衰减 / 行为聚合 / 注入文案 /
配置视图 / 稳定错误码），`services/` 是有状态层（按角色卡分片的 PluginStore 持久化、只读总线
采样、注入决策与投递），本模块只做装配与对外契约面（entry / ui.context / ui.action / llm_tool / timer）。

三条硬约束（源码核实，见 DESIGN.md「已知陷阱」）：
1. 角色归属只取本次调用注入的 `_ctx["lanlan_name"]`，**绝不**回退 `ctx._current_lanlan`
   ——那是上一次调用残留的脏值，会把 A 角色的互动记到 B 角色头上。
2. timer 每拍 `asyncio.run` 新建 event loop 且无 watchdog：tick 内不持有跨拍对象，异常自己兜。
3. `push_message` 只在 tick / 入口 / 工具里发，不在 `startup` 里发（启动期的推送会落在
   ProactiveBridge 订阅窗口之前被静默丢弃）。
"""

from __future__ import annotations

import time
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)

from .core import (
    STAT_NAMES,
    TRIGGER_COMPANY,
    OurLifeSettings,
    advance_streak,
    apply_day_greet,
    apply_decay,
    apply_neglect,
    apply_streak_bonus,
    apply_turn_gain,
    build_text,
    clamp_value,
    local_day,
    neglect_entitlement_days,
    streak_milestone_bonus,
    tier_transitions,
)
from .services import BehaviorSampler, Injector, ShardState, StateStore

# 真实节奏由配置 `[our_life].tick_seconds` 决定；装饰器的 seconds 必须是**字面量正整数**
# （校验器静态检查 Name 节点会拒），所以这里钉 30 秒当"心跳上限"，
# handler 内部按配置节流：太早的拍直接 skip。
_PERSISTED_RESCAN_EVERY = 20  # 每 N 拍重扫一次 store 里的分片键（重启后/外部新增的兜底）
_SESSION_IDLE_SEC = 1800.0  # 超过这么久没说话，算新会话（同会话递减收益据此重置）

__all__ = ["OurLifePlugin"]


@neko_plugin
class OurLifePlugin(NekoPluginBase):
    """养成系数值插件：好感度 / 心情 / 健康 + 强注入。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._settings = OurLifeSettings()
        self._store = StateStore(self, logger=self.logger)
        self._sampler = BehaviorSampler(self, logger=self.logger)
        self._injector = Injector(self, logger=self.logger)
        self._last_tick_at = 0.0
        self._tick_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._reload_settings()
        known = await self._store.list_persisted_lanlans()
        now = time.time()
        normalized = 0
        for lanlan in known:
            state = await self._store.load(lanlan, now=now)
            if not self._settings.enabled and state.last_decay_at != now:
                # 冻结期跨重启的归一化：把折算基准点推到"现在"并落盘一次。
                # 不做这一步的话，关着开关放几天再打开，会一次性补算整段衰减——
                # 那等于惩罚用户"关掉了插件"。只在启动时写一次，不用每拍写。
                state.last_decay_at = now
                await self._store.save(state, now=now)
                normalized += 1
        self.logger.info(
            "our_life ready: enabled={} tick={}s shards={} normalized={} store={}",
            self._settings.enabled,
            self._settings.tick_seconds,
            len(known),
            normalized,
            hasattr(self, "store"),
        )
        return Ok({"status": "ready", "enabled": self._settings.enabled, "shards": len(known)})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        now = time.time()
        saved = 0
        for lanlan in self._store.known_lanlans():
            state = self._store.cached.get(lanlan)
            if state is None:
                continue
            if await self._store.save(state, now=now):
                saved += 1
        self.logger.info("our_life stopped: saved {} shard(s)", saved)
        return Ok({"status": "stopped", "saved": saved})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._reload_settings()
        self.logger.info("our_life config reloaded: enabled={}", self._settings.enabled)
        return Ok({"status": "config_updated", "enabled": self._settings.enabled})

    # ------------------------------------------------------------------
    # 后台心跳：采样 → 结算 → 注入
    # ------------------------------------------------------------------

    @timer_interval(id="tick", seconds=30, name="our_life tick")
    async def on_tick(self, **_):
        now = time.time()
        interval = max(1.0, float(self._settings.tick_seconds))
        if now - self._last_tick_at < interval:
            return Ok({"skipped": True})
        self._last_tick_at = now
        self._tick_count += 1

        try:
            records = await self._sampler.fetch()
            roles = set(self._store.known_lanlans())
            roles.update(self._sampler.discover_lanlans(records))
            if self._tick_count % _PERSISTED_RESCAN_EVERY == 1:
                roles.update(await self._store.list_persisted_lanlans())
            for lanlan in sorted(roles):
                await self._settle(lanlan, records, now=now)
        except Exception:
            # timer 没有 watchdog、异常不会停表：这里兜住，下一拍照常
            self.logger.warning("our_life tick failed", exc_info=True)
            return Ok({"status": "tick_failed"})
        return Ok({"status": "tick_done", "roles": len(roles)})

    async def _settle(self, lanlan: str, records: tuple[Any, ...], *, now: float) -> None:
        """一个角色卡的一拍：惰性衰减 → 行为结算 → 冷落结算 → 落盘 → 注入判定。"""
        if not lanlan:
            return
        state = await self._store.load(lanlan, now=now)
        if not self._settings.enabled:
            # 总开关关闭 = 完全冻结：不采样、不结算、不注入。
            # 只把折算基准点往前推（**内存里**，不落盘，避免禁用在此时每拍写一次 store），
            # 免得重新打开时一次性补算一大段衰减——那会像"惩罚用户关掉了插件"。
            state.last_decay_at = now
            return

        settings = self._settings
        before_stats = state.stats
        elapsed_hours = max(0.0, (now - state.last_decay_at) / 3600.0)
        stats = apply_decay(before_stats, elapsed_hours=elapsed_hours, decay=settings.decay)

        summary = self._sampler.summarize_for(
            records, lanlan=lanlan, seen_ids=state.seen_conversation_ids
        )
        had_new = summary.user_turns > 0
        is_new_day = False

        if had_new:
            # 同会话递减收益：距上次互动超过 _SESSION_IDLE_SEC 就当新会话，重新从满步长开始
            if (
                summary.last_user_at is not None
                and state.last_touch_at is not None
                and (summary.last_user_at - state.last_touch_at) > _SESSION_IDLE_SEC
            ):
                state.session_turns = 0
            stats = apply_turn_gain(stats, session_index=state.session_turns, growth=settings.growth)
            today = local_day(summary.last_user_at if summary.last_user_at is not None else now)
            streak, is_new_day, _broken = advance_streak(
                state.last_active_date, today, state.streak_days
            )
            if is_new_day:
                stats = apply_day_greet(stats, settings.growth)
                fresh = streak_milestone_bonus(streak, awarded=state.milestones, growth=settings.growth)
                if fresh:
                    stats = apply_streak_bonus(stats, milestones=fresh, growth=settings.growth)
                    state.milestones = tuple(sorted({*state.milestones, *fresh}))
            state.streak_days = streak
            state.last_active_date = today
            # 人回来了：冷落账清零（下次断联从新的 last_touch 重新起算）
            state.neglect_days_applied = 0.0

        reference_touch = state.last_touch_at if state.last_touch_at is not None else state.last_decay_at
        gap_hours = max(0.0, (now - reference_touch) / 3600.0)
        entitlement = neglect_entitlement_days(gap_hours, grace_hours=settings.neglect.grace_hours)
        delta_days = entitlement - state.neglect_days_applied
        if delta_days > 0.0 and not had_new:
            stats = apply_neglect(stats, delta_days=delta_days, neglect=settings.neglect)
            state.neglect_days_applied = entitlement

        transitions = tier_transitions(before_stats, stats)
        state.stats = stats
        state.last_decay_at = now
        state.apply_summary(summary)

        plan = self._injector.plan_for_tick(
            state=state,
            settings=settings,
            now=now,
            transitions=transitions,
            had_new_interaction=had_new,
            is_new_day=is_new_day,
        )
        if plan is not None:
            submitted = await self._injector.emit(plan)
            if submitted:
                state.note_injection(
                    at=now, trigger=plan.trigger, summary=_summarize_tiers(state), stats=state.stats
                )
        await self._store.save(state, now=now)

    # ------------------------------------------------------------------
    # 入口（面板 + Agent + 命令面板）
    # ------------------------------------------------------------------

    @ui.action(
        id="status",
        label=tr("actions.status.label", default="Refresh"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="status",
        name=tr("entries.status.name", default="查看我们的生活状态"),
        description=tr("entries.status.description", default="返回当前角色卡的好感度 / 心情 / 健康与连续相处天数"),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["note", "tiers", "streak_days"],
        metadata={"result_kind": "event"},
    )
    async def status_entry(self, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        state = await self._load_for_read(lanlan)
        snapshot = state.snapshot_for_panel(now=time.time())
        return Ok(
            {
                "note": "stats_loaded",
                "lanlan": lanlan,
                "enabled": self._settings.enabled,
                **snapshot,
            }
        )

    @ui.action(
        id="tune",
        label=tr("actions.tune.label", default="Apply"),
        tone="default",
        refresh_context=True,
    )
    @plugin_entry(
        id="tune",
        name=tr("entries.tune.name", default="手动调整数值"),
        description=tr("entries.tune.description", default="把某项数值直接设为给定值（用于纠偏或调试）"),
        input_schema={
            "type": "object",
            "properties": {
                "stat": {
                    "type": "string",
                    "enum": list(STAT_NAMES),
                    "description": tr("fields.stat", default="要调整的数值"),
                },
                "value": {
                    "type": "number",
                    "description": tr("fields.value", default="目标值（0-100）"),
                },
            },
            "required": ["stat", "value"],
        },
        llm_result_fields=["note", "stat", "value"],
    )
    async def tune_entry(self, stat: str = "", value: float = 0.0, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        if stat not in STAT_NAMES:
            return Err(SdkError("invalid_stat"))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return Err(SdkError("invalid_value"))
        state = await self._load_for_read(lanlan)
        state.stats = state.stats.with_value(stat, clamp_value(float(value)))
        await self._store.save(state, now=time.time())
        return Ok({"note": "stat_updated", "stat": stat, "value": round(getattr(state.stats, stat), 2)})

    @ui.action(
        id="reset",
        label=tr("actions.reset.label", default="Reset"),
        tone="danger",
        refresh_context=True,
    )
    @plugin_entry(
        id="reset",
        name=tr("entries.reset.name", default="重置我们的数值"),
        description=tr("entries.reset.description", default="把当前角色卡的数值清回初始状态"),
        input_schema={"type": "object", "properties": {}},
    )
    async def reset_entry(self, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        now = time.time()
        fresh = ShardState(lanlan=lanlan, last_decay_at=now, updated_at=now)
        await self._store.save(fresh, now=now)
        return Ok({"note": "stats_reset", "lanlan": lanlan})

    @ui.action(
        id="switch",
        label=tr("actions.switch.label", default="Toggle"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="switch",
        name=tr("entries.switch.name", default="开关我们的生活"),
        description=tr("entries.switch.description", default="fail-closed 总开关：关闭后数值冻结、不注入、不衰减"),
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": tr("fields.enabled", default="是否开启"),
                }
            },
            "required": ["enabled"],
        },
        llm_result_fields=["note", "enabled"],
    )
    async def switch_entry(self, enabled: bool = False, **kwargs: Any):
        if not isinstance(enabled, bool):
            return Err(SdkError("invalid_value"))
        try:
            await self.config.set("our_life.enabled", bool(enabled))
        except Exception:
            self.logger.warning("failed to persist [our_life].enabled", exc_info=True)
            return Err(SdkError("config_unavailable"))
        await self._reload_settings()
        return Ok({"note": "enabled" if enabled else "disabled", "enabled": self._settings.enabled})

    # ------------------------------------------------------------------
    # 面板上下文
    # ------------------------------------------------------------------

    @ui.context(id="dashboard", title=tr("panel.title", default="我们的生活"))
    async def dashboard_context(self, **kwargs: Any) -> dict[str, Any]:
        lanlan, _error = await self._resolve_lanlan(kwargs, strict=False)
        now = time.time()
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        payload: dict[str, Any] = {
            "enabled": self._settings.enabled,
            "lanlan": lanlan,
            "shards": list(self._store.known_lanlans()),
            "store_available": getattr(self, "store", None) is not None,
            "config": {
                "tick_seconds": self._settings.tick_seconds,
                "mood_tau_hours": self._settings.decay.mood_tau_hours,
                "mood_rest_baseline": self._settings.decay.mood_rest_baseline,
                "health_tau_hours": self._settings.decay.health_tau_hours,
                "health_rest_baseline": self._settings.decay.health_rest_baseline,
                "affection_tau_days": self._settings.decay.affection_tau_days,
                "grace_hours": self._settings.neglect.grace_hours,
                "min_interval_sec": self._settings.inject.min_interval_sec,
                "max_per_hour": self._settings.inject.max_per_hour,
                "max_chars": self._settings.inject.max_chars,
                "respond_on_crisis": self._settings.inject.respond_on_crisis,
            },
        }
        if not lanlan:
            payload["state"] = None
            payload["recent_injections"] = []
            payload["error_code"] = "invalid_lanlan"
            return payload
        state = await self._load_for_read(lanlan)
        payload["state"] = state.snapshot_for_panel(now=now)
        payload["recent_injections"] = [dict(item) for item in state.inject_history[-8:]]
        payload["hours"] = list(state.hour_histogram)
        return payload

    # ------------------------------------------------------------------
    # LLM 工具：她可以自主调用
    # ------------------------------------------------------------------

    @llm_tool(
        name="our_life_feel",
        description=(
            "查询你自己此刻的身心状态（心情 / 身体 / 与主人的关系档位），"
            "用来决定说话的语气、长短与是否主动撒娇。只在你想知道自己'现在什么状态'时调用。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=10.0,
    )
    async def our_life_feel(self, **kwargs: Any) -> dict[str, Any]:
        lanlan = _lanlan_from_kwargs(kwargs)
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        if not lanlan:
            return {"reliable": False, "reason": "invalid_lanlan"}
        state = await self._load_for_read(lanlan)
        return {
            "affection": round(state.stats.affection, 1),
            "mood": round(state.stats.mood, 1),
            "health": round(state.stats.health, 1),
            "tiers": state.stats.tier_map(),
            "streak_days": state.streak_days,
            "feeling": build_text(
                stats=state.stats,
                trigger="interval",
                streak_days=state.streak_days,
                max_chars=self._settings.inject.max_chars,
            ),
            "note": "这是你自己的状态，直接用语气体现，不要报数字或档位名称。",
        }

    @llm_tool(
        name="our_life_company",
        description=(
            "当你确实想让他陪你一会儿时，用它主动开口。会真的打扰到他，所以只在有理由时用；"
            "有冷却时间，冷却中调用会失败。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "你为什么想让他陪你（一句话，给自己看）"}
            },
            "required": [],
        },
        timeout=15.0,
    )
    async def our_life_company(self, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self._settings.enabled:
            # 总开关关着：不要用"冷却中"糊弄模型，语义要分明
            return {"ok": False, "reason": "not_enabled"}
        lanlan = _lanlan_from_kwargs(kwargs)
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        if not lanlan:
            return {"ok": False, "reason": "invalid_lanlan"}
        now = time.time()
        state = await self._store.load(lanlan, now=now)
        plan = self._injector.plan_for_company(state=state, settings=self._settings, now=now)
        if plan is None:
            return {"ok": False, "reason": "company_cooldown"}
        submitted = await self._injector.emit(plan)
        if not submitted:
            return {"ok": False, "reason": "transport_unavailable"}
        state.company_last_at = now
        state.note_injection(at=now, trigger=TRIGGER_COMPANY, summary=_summarize_tiers(state), stats=state.stats)
        await self._store.save(state, now=now)
        self.logger.info("company requested (reason_chars={})", len(reason or ""))
        return {"ok": True, "reason": "sent"}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _reload_settings(self) -> OurLifeSettings:
        try:
            config = await self.config.dump()
        except Exception:
            self.logger.warning("config dump failed; keeping previous settings", exc_info=True)
            return self._settings
        self._settings = OurLifeSettings.from_config(config if isinstance(config, dict) else {})
        return self._settings

    async def _load_for_read(self, lanlan: str) -> ShardState:
        return await self._store.load(lanlan, now=time.time())

    async def _resolve_lanlan(
        self, kwargs: dict[str, Any], *, strict: bool = True
    ) -> tuple[str, Any | None]:
        """解析本次调用属于哪个角色卡。

        **只用本次调用注入的 `_ctx["lanlan_name"]`**；缺失时（且全局只有唯一一个分片）才退化到
        那个分片——唯一分片既可能是内存缓存里的，也可能是还没被加载、只在 store 里躺着的
        （面板早于第一次 tick 打开就是这种情况），所以缓存空时要回查一次持久化分片。
        绝不读 `ctx._current_lanlan`——那是上一次调用残留的脏值。
        """
        lanlan = _lanlan_from_kwargs(kwargs)
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        if not lanlan and strict:
            return "", Err(SdkError("invalid_lanlan"))
        return lanlan, None

    async def _single_known_lanlan(self) -> str:
        """全局唯一分片名（内存缓存优先，缓存空则回查 store）；否则空串。"""
        known = self._store.known_lanlans()
        if not known:
            known = await self._store.list_persisted_lanlans()
        return known[0] if len(known) == 1 else ""


def _lanlan_from_kwargs(kwargs: dict[str, Any]) -> str:
    """从入口/工具收到的 `_ctx` 里取角色名（宿主每次调用都会注入）。"""
    ctx_obj = kwargs.get("_ctx") if isinstance(kwargs, dict) else None
    if isinstance(ctx_obj, dict):
        name = ctx_obj.get("lanlan_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _summarize_tiers(state: ShardState) -> str:
    """给面板/历史一条不含隐私正文的摘要：只有档位。"""
    tiers = state.stats.tier_map()
    return " / ".join(f"{name}={tier}" for name, tier in tiers.items())
