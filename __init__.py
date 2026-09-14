"""我们的生活 (our_life) —— 养成系陪伴插件。

架构（一段话）：`core/` 是零 SDK 依赖的纯函数层（数值模型 / 惰性衰减 / 作息节律 / 经济账本 /
行为聚合 / 注入文案 / 配置视图 / 稳定错误码），`services/` 是有状态层（按角色卡分片的
PluginStore 持久化、只读总线采样、注入决策与投递），本模块只做装配与对外契约面
（entry / ui.context / ui.action / llm_tool / timer）。

v0.2.0「过日子」主线：数值从三轴扩到五轴（+饱食 / 精力），引入作息节律与金币经济，
她每天会**自动吃掉背包里的口粮**——主人囤得够、按时回来，日子就过得下去；
口粮吃完了而主人没回来，她就会饿。面板的「口粮顾问」把这笔账摊开：
每天几餐、还够几天、缺口多少。

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
    ITEM_ORDER,
    STAT_NAMES,
    TRIGGER_COMPANY,
    OurLifeSettings,
    Stats,
    advance_streak,
    advise,
    anniversary_of,
    apply_anniversary,
    apply_coupling,
    apply_day_greet,
    apply_decay,
    apply_item,
    apply_meal,
    apply_neglect,
    apply_streak_bonus,
    apply_turn_gain,
    build_text,
    bump_meal_day,
    clamp_value,
    crisis_axes,
    daily_income,
    effects_for,
    eventful_tier_transitions,
    is_crisis,
    item,
    local_day,
    meal_need_per_day,
    meal_plan,
    meal_restore,
    minutes_until_next_boundary,
    neglect_entitlement_days,
    overlap_hours,
    recharge_plan,
    resolve_rhythm,
    satiety_per_day,
    streak_milestone_bonus,
)
from .services import BehaviorSampler, Injector, ShardState, StateStore, day_number_for

# 真实节奏由配置 `[our_life].tick_seconds` 决定；装饰器的 seconds 必须是**字面量正整数**
# （校验器静态检查 Name 节点会拒），所以这里钉 30 秒当"心跳上限"，
# handler 内部按配置节流：太早的拍直接 skip。
_PERSISTED_RESCAN_EVERY = 20  # 每 N 拍重扫一次 store 里的分片键（重启后/外部新增的兜底）
_SESSION_IDLE_SEC = 1800.0  # 超过这么久没说话，算新会话（同会话递减收益据此重置）
# 单次折算的上限：机器睡了三天再打开，不该让她"饿三天"式的暴跌
# （冷落另有按天的惩罚通道；这里只防数值在开机的瞬间崩掉）
_MAX_ELAPSED_HOURS = 48.0

__all__ = ["OurLifePlugin"]


@neko_plugin
class OurLifePlugin(NekoPluginBase):
    """养成系数值插件：饱食 / 精力 / 心情 / 健康 / 好感 + 金币口粮 + 强注入。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._settings = OurLifeSettings()
        self._store = StateStore(self, logger=self.logger)
        self._sampler = BehaviorSampler(self, logger=self.logger)
        self._injector = Injector(self, logger=self.logger)
        self._last_tick_at = 0.0
        self._tick_count = 0
        # 已经落过盘的分片（每个角色卡每进程只 bootstrap 一次，避免面板刷新写 store）
        self._bootstrapped: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._reload_settings()
        known = await self._store.list_persisted_lanlans()
        now = time.time()
        normalized = 0
        self._bootstrapped.update(known)
        for lanlan in known:
            state = await self._store.load(
                lanlan, now=now, default_sodas=self._settings.economy.start_sodas
            )
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
    # 后台心跳：采样 → 作息折算 → 进食 → 经济 → 注入
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
            if not roles:
                self._warn_no_roles(records)
            for lanlan in sorted(roles):
                await self._settle(lanlan, records, now=now)
        except Exception:
            # timer 没有 watchdog、异常不会停表：这里兜住，下一拍照常
            self.logger.warning("our_life tick failed", exc_info=True)
            return Ok({"status": "tick_failed"})
        return Ok({"status": "tick_done", "roles": len(roles)})

    async def _settle(self, lanlan: str, records: tuple[Any, ...], *, now: float) -> None:
        """一个角色卡的一拍：作息折算 → 行为结算 → 进食 → 经济 → 落盘 → 注入判定。"""
        if not lanlan:
            return
        settings = self._settings
        state = await self._store.load(lanlan, now=now, default_sodas=settings.economy.start_sodas)
        if not settings.enabled:
            # 总开关关闭 = 完全冻结：不采样、不结算、不进食、不注入。
            # 只把折算基准点往前推（**内存里**，不落盘，避免禁用在此时每拍写一次 store），
            # 免得重新打开时一次性补算一大段衰减——那会像"惩罚用户关掉了插件"。
            state.last_decay_at = now
            return

        before_stats = state.stats
        rhythm = self._rhythm(now=now)
        state.sleeping = rhythm.sleeping

        # 1) 惰性衰减（按真实流逝时间折算，且区分清醒/睡眠）+ 跨轴耦合（饿→心情掉得快，累→健康恢复慢）
        elapsed_hours = min(max(0.0, (now - state.last_decay_at) / 3600.0), _MAX_ELAPSED_HOURS)
        stats = apply_decay(
            stats=before_stats,
            elapsed_hours=elapsed_hours,
            decay=settings.decay,
            rhythm=rhythm,
            awake_hours=self._awake_hours(now=now, elapsed_hours=elapsed_hours, rhythm=rhythm),
        )
        stats = apply_coupling(stats, elapsed_hours=elapsed_hours, decay=settings.decay)

        # 2) 行为采样 → 成长 / 连续天数 / 里程碑
        summary = self._sampler.summarize_for(
            records, lanlan=lanlan, seen_ids=state.seen_conversation_ids
        )
        had_new = summary.user_turns > 0
        is_new_day = False
        streak = state.streak_days
        today = local_day(summary.last_user_at if summary.last_user_at is not None else now)

        if had_new:
            # 同会话递减收益：距上次互动超过 _SESSION_IDLE_SEC 就当新会话，重新从满步长开始
            if (
                summary.last_user_at is not None
                and state.last_touch_at is not None
                and (summary.last_user_at - state.last_touch_at) > _SESSION_IDLE_SEC
            ):
                state.session_turns = 0
            stats = apply_turn_gain(stats, session_index=state.session_turns, growth=settings.growth)
            streak, is_new_day, _broken = advance_streak(state.last_active_date, today, state.streak_days)
            if is_new_day:
                stats = apply_day_greet(stats, settings.growth)
                fresh = streak_milestone_bonus(streak, awarded=state.milestones, growth=settings.growth)
                if fresh:
                    stats = apply_streak_bonus(stats, milestones=fresh, growth=settings.growth)
                    state.milestones = tuple(sorted({*state.milestones, *fresh}))
                    state.sodas += _streak_sodas(fresh, settings=settings)
            state.streak_days = streak
            state.last_active_date = today
            if not state.first_day:
                # 相遇日：相处天数与纪念日都按它算（断档也不会错位）
                state.first_day = today
            # 人回来了：冷落账清零（下次断联从新的 last_touch 重新起算）
            state.neglect_days_applied = 0.0

        # 3) 冷落惩罚（只扣心情/健康/好感；饱食与精力由作息驱动，见 core/model.apply_neglect）
        reference_touch = state.last_touch_at if state.last_touch_at is not None else state.last_decay_at
        gap_hours = max(0.0, (now - reference_touch) / 3600.0)
        entitlement = neglect_entitlement_days(gap_hours, grace_hours=settings.neglect.grace_hours)
        delta_days = entitlement - state.neglect_days_applied
        if delta_days > 0.0 and not had_new:
            stats = apply_neglect(stats, delta_days=delta_days, neglect=settings.neglect)
            state.neglect_days_applied = entitlement

        # 4) 金币与日账（跨天重置消费上限、发放零花钱、结算日薪）
        self._settle_account(
            state,
            today=today,
            turns=summary.user_turns if had_new else 0,
            is_new_day=is_new_day,
            streak=streak,
        )

        # 5) 吃饭：她自己去背包里吃（主人囤得够不够，就在这一步见分晓）
        stats = self._auto_meal(state, stats=stats, now=now, day=today, rhythm=rhythm)

        # 6) 纪念日（相处天数命中锚点）：当天第一次结算时给一次性礼物，并让模型知道今天是纪念日
        day_number = day_number_for(state, today)
        anniversary = None if day_number <= 0 or not state.first_day else anniversary_of(state.first_day, today)
        anniversary_fresh = anniversary is not None and day_number != state.day_number_seen
        if anniversary_fresh:
            stats = apply_anniversary(
                stats,
                mood=settings.rhythm.anniversary_mood,
                affection=settings.rhythm.anniversary_affection,
            )
        state.day_number_seen = day_number

        # 跨档判据用 `eventful_tier_transitions`（带 0.05 分迟滞），不是硬比较的
        # `tier_transitions`：默认好评 20.0 正好压着 stranger/acquainted 的分界线，
        # 硬比较会把第一拍的 19.9998456796 判成跨档，白送一次 tier_change 强注入
        # （真机 store 里抓到过，详见 core/model.py 的 TIER_CROSSING_MARGIN）。
        transitions = eventful_tier_transitions(before_stats, stats)
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
            rhythm=rhythm,
            anniversary=anniversary,
            anniversary_seen=not anniversary_fresh,
        )
        if plan is not None:
            submitted = await self._injector.emit(plan)
            if submitted:
                state.note_injection(
                    at=now, trigger=plan.trigger, summary=_summarize_tiers(state), stats=state.stats
                )
                if plan.wants_reply:
                    state.note_respond(at=now)
        await self._store.save(state, now=now)

    # ------------------------------------------------------------------
    # 结算内部件
    # ------------------------------------------------------------------

    def _rhythm(self, *, now: float):
        return resolve_rhythm(
            now,
            sleep_start_hour=self._settings.rhythm.sleep_start_hour,
            sleep_end_hour=self._settings.rhythm.sleep_end_hour,
        )

    def _awake_hours(self, *, now: float, elapsed_hours: float, rhythm: Any) -> float:
        """`elapsed_hours` 里她醒着的小时数。

        正常一拍（间隔很短）直接用"睡没睡"判断，避免每 30 秒都去逐时推进；
        间隔较长时用 `elapsed_hours × 清醒占比` 近似；只有间隔超过一天（关机后重开）
        才走 `core.rhythm.overlap_hours` 精算——那种情况下"她到底睡了多久"会显著影响结果，
        值得多花那次循环。
        """
        if elapsed_hours <= 0.0:
            return 0.0
        if elapsed_hours <= 1.0:
            return elapsed_hours * (0.0 if rhythm.sleeping else 1.0)
        if elapsed_hours <= 26.0:
            return elapsed_hours * max(0.0, min(1.0, rhythm.awake_ratio))
        return overlap_hours(
            _local_datetime(now - elapsed_hours * 3600.0),
            _local_datetime(now),
            sleep_start_hour=self._settings.rhythm.sleep_start_hour,
            sleep_end_hour=self._settings.rhythm.sleep_end_hour,
        )

    def _settle_account(
        self,
        state: ShardState,
        *,
        today: str,
        turns: int,
        is_new_day: bool,
        streak: int,
    ) -> None:
        """跨天重置 + 零花钱 + 日薪。"""
        economy = self._settings.economy
        if state.account_date != today:
            state.account_date = today
            state.daily_spent = 0
            state.daily_allowance_granted = False
        if not economy.enabled:
            return
        if turns > 0:
            state.sodas += daily_income(
                turns=turns,
                is_new_day=is_new_day,
                streak_days=streak,
                turn_reward=economy.turn_reward,
                new_day_bonus=economy.new_day_bonus,
                streak_reward=economy.streak_reward,
                max_per_session=economy.max_turns_per_session,
            )
        if not state.daily_allowance_granted:
            state.sodas += max(0, int(economy.daily_allowance))
            state.daily_allowance_granted = True

    def _auto_meal(
        self, state: ShardState, *, stats: Stats, now: float, day: str, rhythm: Any
    ) -> Stats:
        """从背包里自动吃掉口粮（够就吃、不够就饿着）。返回折算后的数值。

        节奏由 `core.model` 的饱食衰减与 `MEAL_THRESHOLD` 共同决定：清醒时约每 10 小时
        一餐，所以她每天会自己吃掉 2~3 份口粮（面板的顾问面板会把这个数摊开给主人看）。
        一次 tick 最多吃一餐——她不会"一口气把一周的饭塞下去"。

        **睡眠期照吃**：一天里就只有"饿到阈值"这一道闸，不额外加"必须醒着"——
        否则睡眠窗（默认 8 小时掉 12 分）会把饱食压到阈值以下，她一醒来就同时在
        "饿着"和"没吃过"，感觉像随机的。代价是"她睡着时背包少了一份"，
        这在面板的账本上是可见且说得通的（她睡前吃了/醒了吃），比数值上说不通要好。
        """
        economy = self._settings.economy
        _ = rhythm  # 保留参数位：将来若要按作息区分"正餐/宵夜"，入口已经在这里
        if not economy.enabled:
            return stats
        chosen = meal_plan(
            state.inventory,
            staple_item_id=economy.staple_item_id,
            satiety=stats.satiety,
            threshold=economy.meal_threshold,
        )
        if chosen is None:
            return stats
        found = item(chosen)
        stats = apply_meal(
            stats,
            satiety=meal_restore(chosen) if found is None or found.food else 0.0,
            effects=effects_for(chosen),
        )
        state.inventory = state.inventory.with_consumed(chosen, 1)
        state.meal_days = bump_meal_day(state.meal_days, day)
        state.meals_total += 1
        state.last_meal_at = now
        self.logger.info(
            "our_life: ate {} (satiety -> {}, stock -> {})",
            chosen,
            round(stats.satiety, 1),
            state.inventory.food_units(),
        )
        return stats

    def _anniversary(self, state: ShardState, *, today: str):
        """今天的纪念日（相处天数命中锚点）；没有相遇日期时为 None。"""
        if not state.first_day or not today:
            return None
        return anniversary_of(state.first_day, today)

    async def _runtime_view(self, state: ShardState, *, now: float) -> dict[str, Any]:
        """面板/入口共用的"她今天过得怎么样"视图（不含任何用户原文）。"""
        settings = self._settings
        rhythm = self._rhythm(now=now)
        per_day = satiety_per_day(rhythm=rhythm)
        advisor = self._advisor(state, per_day=per_day)
        return {
            "rhythm": rhythm.as_dict(),
            "phase": rhythm.phase,
            "sleeping": rhythm.sleeping,
            "minutes_to_boundary": minutes_until_next_boundary(now, rhythm),
            "per_day_decay": round(per_day, 2),
            "advisor": advisor.as_dict(),
            "crisis": is_crisis(state.stats, settings.inject),
            "crisis_axes": list(crisis_axes(state.stats, settings.inject)),
            "day_number": day_number_for(state, rhythm.date_iso),
            "anniversary": _anniversary_dict(self._anniversary(state, today=rhythm.date_iso)),
        }

    def _advisor(self, state: ShardState, *, per_day: float) -> Any:
        economy = self._settings.economy
        meal_need = meal_need_per_day(per_day_decay=per_day, threshold=economy.meal_threshold)
        return advise(
            inventory=state.inventory,
            coins=state.sodas,
            meal_need=meal_need,
            observed_meals_per_day=state.meals_per_day(),
            staple_item_id=economy.staple_item_id,
            horizon_days=economy.advisor_horizon_days,
            warn_days=economy.advisor_warn_days,
        )

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
        description=tr(
            "entries.status.description",
            default="返回当前角色卡的五项数值、金币、口粮与连续相处天数",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["note", "tiers", "streak_days", "sodas"],
        metadata={"result_kind": "event"},
    )
    async def status_entry(self, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        now = time.time()
        state = await self._touch_shard(lanlan, now=now)
        snapshot = state.snapshot_for_panel(now=now)
        runtime = await self._runtime_view(state, now=now)
        return Ok(
            {
                # 通道缺席时如实回码：数值只在内存里、重启就丢，别让面板装作一切正常。
                # `dashboard_context` 会把同一个码放进 `error_code`，面板据此显示横幅。
                "note": "stats_loaded" if self._store.store_available else "store_unavailable",
                "lanlan": lanlan,
                "enabled": self._settings.enabled,
                "store_available": self._store.store_available,
                **snapshot,
                **runtime,
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
        return Ok(
            {
                "note": "stat_updated" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "stat": stat,
                "value": round(getattr(state.stats, stat), 2),
            }
        )

    @ui.action(
        id="feed",
        label=tr("actions.feed.label", default="Care"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="feed",
        name=tr("entries.feed.name", default="照顾她"),
        description=tr(
            "entries.feed.description",
            default="喂饭 / 吃药 / 陪玩 / 送礼物：用掉背包里的一件东西，并立刻作用到数值上",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "enum": list(ITEM_ORDER),
                    "description": tr("fields.item", default="要用哪件东西"),
                }
            },
            "required": ["item"],
        },
        llm_result_fields=["note", "item", "satiety"],
    )
    async def feed_entry(self, item: str = "", **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        catalog_item = _item_of(item)
        if catalog_item is None:
            return Err(SdkError("invalid_item"))
        now = time.time()
        state = await self._load_for_read(lanlan)
        if state.inventory.get(catalog_item.id) <= 0:
            # 背包里没有：如实回"没货"（面板把它翻成"背包里没有这件东西"），
            # 而不是假装成功——假成功会让用户以为用了但数值没动。
            return Err(SdkError("invalid_item"))
        state.stats = apply_item(state.stats, effects_for(catalog_item.id))
        state.inventory = state.inventory.with_consumed(catalog_item.id, 1)
        if catalog_item.food:
            # 主动喂的也算一餐：面板的"每天吃几餐"要把它计入，否则实测值会偏低
            state.meal_days = bump_meal_day(state.meal_days, local_day(now))
            state.meals_total += 1
            state.last_meal_at = now
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "care_applied" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "item": catalog_item.id,
                "inventory": state.inventory_counts(),
                "satiety": round(state.stats.satiety, 2),
                "mood": round(state.stats.mood, 2),
                "energy": round(state.stats.energy, 2),
                "health": round(state.stats.health, 2),
            }
        )

    @ui.action(
        id="shop",
        label=tr("actions.shop.label", default="Buy"),
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="shop",
        name=tr("entries.shop.name", default="买点东西"),
        description=tr("entries.shop.description", default="用金币买口粮 / 药品 / 玩具 / 礼物存进背包"),
        input_schema={
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "enum": list(ITEM_ORDER),
                    "description": tr("fields.item", default="买什么"),
                },
                "quantity": {
                    "type": "integer",
                    "description": tr("fields.quantity", default="买几个"),
                },
            },
            "required": ["item", "quantity"],
        },
        llm_result_fields=["note", "item", "quantity", "sodas"],
    )
    async def shop_entry(self, item: str = "", quantity: int = 0, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        economy = self._settings.economy
        now = time.time()
        state = await self._load_for_read(lanlan)
        plan = recharge_plan(
            item_id=item,
            quantity=quantity,
            inventory=state.inventory,
            coins=state.sodas,
            daily_spent=state.daily_spent,
            daily_limit=economy.shop_daily_limit,
        )
        if not plan.ok:
            return Err(SdkError(plan.reason))
        carried = state.inventory.with_added(plan.item_id, plan.quantity)
        if economy.carry_max > 0 and carried.get(plan.item_id) > economy.carry_max:
            return Err(SdkError("carry_full"))
        state.inventory = carried
        state.sodas -= plan.total_cost
        state.daily_spent += plan.total_cost
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "shop_purchased" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "item": plan.item_id,
                "quantity": plan.quantity,
                "cost": plan.total_cost,
                "sodas": state.sodas,
                "inventory": state.inventory_counts(),
            }
        )

    @ui.action(
        id="allowance",
        label=tr("actions.allowance.label", default="Grant"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="allowance",
        name=tr("entries.allowance.name", default="给她一笔金币"),
        description=tr("entries.allowance.description", default="手动补一笔金币（调试或补偿用）"),
        input_schema={
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": tr("fields.amount", default="补多少（可为负，用于回收）"),
                }
            },
            "required": ["amount"],
        },
        llm_result_fields=["note", "sodas"],
    )
    async def allowance_entry(self, amount: int = 0, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return Err(SdkError("invalid_value"))
        state = await self._load_for_read(lanlan)
        state.sodas = max(0, state.sodas + int(amount))
        await self._store.save(state, now=time.time())
        return Ok(
            {
                "note": "coin_updated" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "sodas": state.sodas,
            }
        )

    @ui.action(
        id="advisor",
        label=tr("actions.advisor.label", default="Advice"),
        tone="info",
        refresh_context=True,
    )
    @plugin_entry(
        id="advisor",
        name=tr("entries.advisor.name", default="她每天要吃多少"),
        description=tr(
            "entries.advisor.description",
            default="算出她每天的饭量、口粮还能撑几天、还缺多少，用来决定囤多少",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["note", "meals_per_day", "stock_meals", "days_remaining"],
    )
    async def advisor_entry(self, **kwargs: Any):
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        now = time.time()
        state = await self._touch_shard(lanlan, now=now)
        runtime = await self._runtime_view(state, now=now)
        return Ok(
            {
                "note": "stats_loaded" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "lanlan": lanlan,
                "sodas": state.sodas,
                "inventory": state.inventory_counts(),
                **runtime,
            }
        )

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
        fresh = ShardState(
            lanlan=lanlan,
            last_decay_at=now,
            updated_at=now,
            sodas=max(0, int(self._settings.economy.start_sodas)),
            account_date=local_day(now),
        )
        await self._store.save(fresh, now=now)
        return Ok(
            {
                "note": "stats_reset" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "lanlan": lanlan,
            }
        )

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
        if not self._store.store_available:
            # 这里**刻意不改语义**：总开关走的是配置层（`self.config.set`），
            # 与数据 store 是两条独立通道，用 store 缺席与否去挡开关会是误报。
            # 但这条信号本身值得留痕（装配异常的一条线索），所以只记日志。
            self.logger.warning(
                "store channel is absent while toggling [our_life].enabled; "
                "the switch still goes through the config channel"
            )
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
        settings = self._settings
        payload: dict[str, Any] = {
            "enabled": settings.enabled,
            "lanlan": lanlan,
            "shards": list(self._store.known_lanlans()),
            "store_available": self._store.store_available,
            "config": {
                "tick_seconds": settings.tick_seconds,
                "mood_tau_hours": settings.decay.mood_tau_hours,
                "mood_rest_baseline": settings.decay.mood_rest_baseline,
                "health_tau_hours": settings.decay.health_tau_hours,
                "health_rest_baseline": settings.decay.health_rest_baseline,
                "affection_tau_days": settings.decay.affection_tau_days,
                "sleep_start_hour": settings.rhythm.sleep_start_hour,
                "sleep_end_hour": settings.rhythm.sleep_end_hour,
                "meals_per_day_theoretical": round(
                    meal_need_per_day(
                        per_day_decay=satiety_per_day(rhythm=self._rhythm(now=now)),
                        threshold=settings.economy.meal_threshold,
                    ),
                    2,
                ),
                "economy_enabled": settings.economy.enabled,
                "staple_item_id": settings.economy.staple_item_id,
                "start_sodas": settings.economy.start_sodas,
                "daily_allowance": settings.economy.daily_allowance,
                "shop_daily_limit": settings.economy.shop_daily_limit,
                "carry_max": settings.economy.carry_max,
                "meal_threshold": settings.economy.meal_threshold,
                "grace_hours": settings.neglect.grace_hours,
                "min_interval_sec": settings.inject.min_interval_sec,
                "max_per_hour": settings.inject.max_per_hour,
                "respond_max_per_hour": settings.inject.respond_max_per_hour,
                "max_chars": settings.inject.max_chars,
                "respond_on_crisis": settings.inject.respond_on_crisis,
                "quiet_during_sleep": settings.inject.quiet_during_sleep,
            },
            "shop": _shop_catalog(),
        }
        if not lanlan:
            payload["state"] = None
            payload["recent_injections"] = []
            payload["error_code"] = "invalid_lanlan"
            return payload
        state = await self._touch_shard(lanlan, now=now)
        payload["state"] = state.snapshot_for_panel(now=now)
        payload["runtime"] = await self._runtime_view(state, now=now)
        payload["recent_injections"] = [dict(item) for item in state.inject_history[-8:]]
        # 近期走势：复用注入历史里已有的数值快照（`note_injection` 落的那一份），
        # 只带 `{at, stats}` 三个非正文键，面板用字符画折线。没有历史就是空列表。
        payload["trend"] = [
            {"at": entry.get("at"), "stats": dict(entry["stats"])}
            for entry in state.inject_history[-12:]
            if isinstance(entry.get("stats"), dict)
        ]
        payload["hours"] = list(state.hour_histogram)
        payload["meal_days"] = [[day, count] for day, count in state.meal_days]
        if not self._store.store_available:
            # 面板据此显示横幅：数值只在内存里、重启就丢。
            # 注意它**不报**"`get`/`set` 返回 Err"那种瞬时降级——那是本插件的既定容错契约
            # （`services/state.py` 模块 docstring），把瞬时故障当"不可用"会让横幅一直挂着。
            payload["error_code"] = "store_unavailable"
        return payload

    # ------------------------------------------------------------------
    # LLM 工具：她可以自主调用
    # ------------------------------------------------------------------

    @llm_tool(
        name="our_life_feel",
        description=(
            "查询你自己此刻的身心状态（肚子饱不饱、精神够不够、心情、身体、与主人的关系档位），"
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
        now = time.time()
        rhythm = self._rhythm(now=now)
        return {
            "affection": round(state.stats.affection, 1),
            "mood": round(state.stats.mood, 1),
            "health": round(state.stats.health, 1),
            "satiety": round(state.stats.satiety, 1),
            "energy": round(state.stats.energy, 1),
            "tiers": state.stats.tier_map(),
            "streak_days": state.streak_days,
            "sleeping": rhythm.sleeping,
            "feeling": build_text(
                stats=state.stats,
                trigger="interval",
                streak_days=state.streak_days,
                max_chars=self._settings.inject.max_chars,
                rhythm=rhythm,
                day_number=state.day_number,
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
        state = await self._store.load(lanlan, now=now, default_sodas=self._settings.economy.start_sodas)
        plan = self._injector.plan_for_company(state=state, settings=self._settings, now=now)
        if plan is None:
            return {"ok": False, "reason": "company_cooldown"}
        submitted = await self._injector.emit(plan)
        if not submitted:
            return {"ok": False, "reason": "transport_unavailable"}
        state.company_last_at = now
        state.note_injection(
            at=now, trigger=TRIGGER_COMPANY, summary=_summarize_tiers(state), stats=state.stats
        )
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
        return await self._store.load(
            lanlan, now=time.time(), default_sodas=self._settings.economy.start_sodas
        )

    async def _touch_shard(self, lanlan: str, *, now: float) -> ShardState:
        """把分片落盘一次（缺则创建），"读路径"也调用。

        为什么读路径也要落盘：tick 是后台定时器、**没有 `_ctx`**，它只能靠
        "已知分片 ∪ 总线记录里的角色名"决定该结算谁。冷装机上这两者都可能是空的
        （总线记录若不带 `lanlan_name`），插件就会一直空转、数值永远不动。
        面板/入口被碰过本身就是"这个角色卡在用"的信号，落一个默认分片，
        tick 从此就有名单了。

        每个角色卡每次进程生命周期内只落一次盘（`_bootstrapped`），
        免得面板每次刷新都写一遍 store。
        """
        state = await self._store.load(
            lanlan, now=now, default_sodas=self._settings.economy.start_sodas
        )
        if not state.account_date:
            state.account_date = local_day(now)
        if lanlan not in self._bootstrapped:
            await self._store.save(state, now=now)
            self._bootstrapped.add(lanlan)
        return state

    def _warn_no_roles(self, records: tuple[Any, ...]) -> None:
        """空转诊断：告诉维护者"总线里看到了记录，但认不出属于哪个角色卡"。

        只记录**键名**（不含任何值），避免把对话正文带进日志。
        """
        if not records or self._tick_count % _PERSISTED_RESCAN_EVERY != 1:
            return
        keys = sorted({str(key) for record in records[:5] for key in record})
        self.logger.warning(
            "our_life: no role resolved from bus conversations (%d records); "
            "waiting for a panel/entry call to bootstrap a shard. record keys=%s",
            len(records),
            keys,
        )

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


def _local_datetime(timestamp: float) -> Any:
    """时间戳 → 本地 `datetime`（`core.rhythm.overlap_hours` 的入参形状）。"""
    from datetime import datetime

    return datetime.fromtimestamp(float(timestamp))


def _item_of(item_id: str) -> Any:
    """按 id 取物品（入口参数名与 `core.economy.item` 同名，这里做个薄包装避免遮蔽）。"""
    from .core.economy import item as catalog_item

    return catalog_item(item_id)


def _summarize_tiers(state: ShardState) -> str:
    """给面板/历史一条不含隐私正文的摘要：只有档位。"""
    tiers = state.stats.tier_map()
    return " / ".join(f"{name}={tier}" for name, tier in tiers.items())


def _streak_sodas(milestones: tuple[int, ...], *, settings: OurLifeSettings) -> int:
    """里程碑附带的金币奖励（按位对应 `streak_sodas_bonus`，越界忽略）。"""
    table = settings.growth.streak_sodas_bonus
    total = 0
    for milestone in milestones:
        try:
            position = settings.growth.streak_milestones.index(milestone)
        except ValueError:
            continue
        if position < len(table):
            total += max(0, int(table[position]))
    return total


def _anniversary_dict(anniversary: Any) -> dict[str, Any] | None:
    """纪念日快照（面板直接渲染；没有就是 None，而不是空 dict）。"""
    return None if anniversary is None else dict(anniversary.as_dict())


def _shop_catalog() -> list[dict[str, Any]]:
    """商店货架（面板直接渲染；价格与效果都来自 `core/economy.ITEMS` 单一来源）。"""
    from .core.economy import ITEM_ORDER, ITEMS

    out: list[dict[str, Any]] = []
    for item_id in ITEM_ORDER:
        entry = ITEMS[item_id]
        out.append(
            {
                "id": entry.id,
                "kind": entry.kind,
                "cost": entry.cost_sodas,
                "food": entry.food,
                "carry_max": entry.carry_max,
                "effects": [[name, delta] for name, delta in entry.effects],
            }
        )
    return out
