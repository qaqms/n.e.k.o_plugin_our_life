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

import random
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
    GAME_KINDS,
    ITEM_ORDER,
    JOB_ORDER,
    JUDGMENT_LABELS,
    STAT_NAMES,
    TRIGGER_COMPANY,
    OurLifeSettings,
    Stats,
    advance_streak,
    advise,
    anniversary_of,
    answer_invalid_reason,
    apply_anniversary,
    apply_coupling,
    apply_day_greet,
    apply_decay,
    apply_item,
    apply_judgment,
    apply_luck,
    apply_meal,
    apply_neglect,
    apply_streak_bonus,
    apply_turn_gain,
    axis_details,
    build_challenge,
    build_text,
    bump_meal_day,
    challenge_public_view,
    clamp_value,
    consume_challenge,
    crisis_axes,
    daily_block_reason,
    daily_income,
    effects_for,
    eventful_tier_transitions,
    is_crisis,
    is_valid_day,
    item,
    job_catalog,
    job_narration,
    judge,
    local_day,
    makeup_reason,
    meal_need_per_day,
    meal_plan,
    meal_restore,
    minutes_until_next_boundary,
    neglect_entitlement_days,
    next_checkin_streak,
    overlap_hours,
    pick_event,
    recharge_plan,
    resolve_rhythm,
    reward_coins,
    roll_luck,
    satiety_per_day,
    settle_shift,
    shift_hits_sleep_window,
    start_block_reason,
    streak_milestone_bonus,
    submit_arith,
    submit_hielo,
    wage_preview,
    week_key,
)
from .core.jobs import apply_shift_costs
from .services import BehaviorSampler, Injector, ShardState, StateStore, ToolWatch, day_number_for

# 真实节奏由配置 `[our_life].tick_seconds` 决定；装饰器的 seconds 必须是**字面量正整数**
# （校验器静态检查 Name 节点会拒），所以这里钉 30 秒当"心跳上限"，
# handler 内部按配置节流：太早的拍直接 skip。
_PERSISTED_RESCAN_EVERY = 20  # 每 N 拍重扫一次 store 里的分片键（重启后/外部新增的兜底）
_SESSION_IDLE_SEC = 1800.0  # 超过这么久没说话，算新会话（同会话递减收益据此重置）
# 单次折算的上限：机器睡了三天再打开，不该让她"饿三天"式的暴跌
# （冷落另有按天的惩罚通道；这里只防数值在开机的瞬间崩掉）
_MAX_ELAPSED_HOURS = 48.0
# 签到的"运气"掷骰用模块级随机源（v0.7.0）：掷骰发生在**后端**，客户端只收到结果，
# 没有作弊面；单测对本实例 seed 即可复现幸运档。
_CHECKIN_RNG = random.Random()
# 玩家打工的出题/抽牌随机源（v0.7.0）：与签到同一手法——真值只在后端，
# 单测对本实例 seed 即可复现题目与牌序。
_GAME_RNG = random.Random()

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
        # 工具注册心跳（v0.5.0）：@llm_tool 只在启动时发一次 IPC，main_server 没就绪
        # 或重启后她的三个工具会静默缺席（见 services/tool_watch.py 模块 docstring）。
        # 与总开关无关：注册韧性是宿主层面的在场性，不随业务冻结而应冻结。
        self._tool_watch = ToolWatch(self, logger=self.logger)
        self._last_tick_at = 0.0
        self._tick_count = 0
        # 面板焦点分片（v0.4.2）：多角色卡时"面板到底看哪张卡"的唯一信号。
        # 优先级在 `_resolve_lanlan`：本次 `_ctx` > 焦点 > 全局唯一分片。
        # 持久化在独立 store 键（见 `services/state.py` 的 `FOCUS_KEY`），重启后仍在。
        self._focus_lanlan = ""
        # 已经落过盘的分片（每个角色卡每进程只 bootstrap 一次，避免面板刷新写 store）
        self._bootstrapped: set[str] = set()
        # 反馈闭环的**待消费判断**：工具可以在任意时刻被调用（不跟 tick 对齐），
        # 而"把修正量加到数值上"必须走 tick 的统一折算链，否则会和惰性衰减打架
        # （长时间没跑 tick 时，直接改数值会被随后的整段折算顺手吃掉）。
        #
        # 存的是**已经算好并已记账的修正量**，不是标签：四道闸门在工具调用那一刻
        # 就全部判定完毕（那时"有没有新互动""还剩多少额度"都是新鲜的），
        # tick 只负责"施加"，**不重新判定**——否则一次互动会被两条路径各判一次，
        # 而第二拍时 `last_touch_at` 没有前进，合法判断会被误判成 `needs_interaction`
        # 而永远施加不上（这是实现期真被自己的门抓到的 bug）。
        # 刻意**不持久化**：重启后一条没被消费的判断丢掉是正确行为。
        self._pending_judgments: dict[str, dict[str, float]] = {}

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
        self._focus_lanlan = await self._store.load_focus()
        if self._focus_lanlan and self._focus_lanlan not in known:
            # 焦点指向已被删的分片：当场清除，不让一个鬼名字占住优先级。
            self._focus_lanlan = ""
            await self._store.save_focus("")
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
            "our_life ready: enabled={} tick={}s shards={} normalized={} focus={} store={}",
            self._settings.enabled,
            self._settings.tick_seconds,
            len(known),
            normalized,
            self._focus_lanlan or "-",
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

        # 每拍都递一下心跳器，内部按 300s 自节流；单独兜异常——巡检坏掉不许
        # 把行为采样 / 衰减结算一起拖下水（tool_watch 自己也不炸，这是双保险）。
        try:
            await self._tool_watch.maybe_run(now=now)
        except Exception:
            self.logger.warning("our_life tool watch leaked", exc_info=True)

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

        # 4) 金币与日账（跨天重置消费上限、发放零花钱、结算日薪）+ 打工日账重置
        self._settle_account(
            state,
            today=today,
            turns=summary.user_turns if had_new else 0,
            is_new_day=is_new_day,
            streak=streak,
        )
        state.reset_job_day(today=today)
        # 玩家打工的日额度账与班次账同一条日界线；入口也会各自 reset（幂等）。
        state.reset_game_day(today=today)

        # 4.5) 打工到点结算（在吃饭之前：下班回来又累又饿，饿了就该吃）。
        # 班次是持久化的真实时间：重启/关机后 tick 下一拍看到 `now >= job_end_at`
        # 照样补结，与惰性衰减同一手法，没有常驻计时器。
        job_outcome = None
        if self._settings.job.enabled and state.working and now >= state.job_end_at:
            settled = self._settle_job(state, stats=stats, now=now)
            if settled is not None:
                job_outcome, stats = settled

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

        # 6.5) 反馈闭环：把"她自己的判断"回流成修正项。
        # 修正量的四道闸门与预算记账都在工具调用那一刻完成了（见 `our_life_judge`），
        # 这里只把**已经批准的增量**加在衰减之后——顺序很重要：先折算再修正，
        # 免得修正量被这一拍的衰减顺手吃掉一截。
        # 同时每拍调一次 `reset_judgment_day`：工具可能先于 tick 被调用，
        # 但面板读到的当日额度必须跟着日界线走。
        if state.reset_judgment_day(today=today):
            self.logger.info("our_life: judgment budget reset for a new day")
        pending = self._pending_judgments.pop(state.lanlan, None)
        judgment_label = ""
        if pending is not None:
            judgment_label = str(pending.get("_label", ""))
            deltas = {name: value for name, value in pending.items() if name in STAT_NAMES}
            if deltas:
                stats = apply_judgment(stats, deltas)

        # 跨档判据用 `eventful_tier_transitions`（带 0.05 分迟滞），不是硬比较的
        # `tier_transitions`：默认好评 20.0 正好压着 stranger/acquainted 的分界线，
        # 硬比较会把第一拍的 19.9998456796 判成跨档，白送一次 tier_change 强注入
        # （真机 store 里抓到过，详见 core/model.py 的 TIER_CROSSING_MARGIN）。
        transitions = eventful_tier_transitions(before_stats, stats)
        state.stats = stats
        state.last_decay_at = now
        state.apply_summary(summary)

        # 6.6) 阶段性事件（v0.4.0）：把她身上"真的发生过"的事挑出来，记一次账。
        #
        # 为什么在 tier_change 之后、注入判定之前：
        # `plan_for_tick` 要看到本拍最终的 `state.stats`，而事件的判定需要**本拍的 before/after**
        # ——两者都只在这一刻同时可得。放到注入判定之后会让事件晚一拍才被记账，
        # 而晚一拍就意味着"她已经好了"与"她在说什么"错开一帧。
        #
        # 顺序纪律（很容易写反，见 core/events 模块 docstring 第 4 条）：
        #   1. **先记账**（无条件）——"要不要记"与"要不要说"是两个问题，
        #      混在一起会让睡过去的事件在下一拍被重新识别一遍（冷却靠的正是这本台账）；
        #   2. **再决定发不发**（睡眠静默 / 同轴危机 / 小时上限都在 `plan_for_event` 里）。
        staged = pick_event(
            before=before_stats,
            after=stats,
            settings=settings.events,
            now=now,
            ledger=state.event_history,
        )
        if staged is not None:
            state.note_event(staged.as_dict(at=now))
            event_plan = self._injector.plan_for_event(
                state=state,
                settings=settings,
                now=now,
                event=staged,
                rhythm=rhythm,
            )
            # 事件注入复用与常规注入同一套投递与记账（`plan.wants_reply` 决定是否再记一次
            # "主动开口"），这样 respond 的小时上限对事件一样生效——
            # 不给事件开第三条频控口子（见 core/events 的"本轮刻意不做"）。
            if event_plan is not None:
                submitted = await self._injector.emit(event_plan)
                if submitted:
                    state.note_injection(
                        at=now, trigger=event_plan.trigger, summary=_summarize_tiers(state), stats=state.stats
                    )
                    if event_plan.wants_reply:
                        state.note_respond(at=now)

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
            judgment_label=judgment_label,
        )
        if job_outcome is not None:
            # 下班叙事：独立通道（与阶段事件同理：同拍都该说话，单链会互顶）。
            # 记账已在 `_settle_job` 完成，这里只决定"要不要把这句话送进上下文"。
            job_plan = self._injector.plan_for_job(
                state=state,
                settings=settings,
                now=now,
                job_line=job_narration(job_outcome.job_id, pay=job_outcome.pay, early=job_outcome.early),
                rhythm=rhythm,
            )
            if job_plan is not None:
                submitted = await self._injector.emit(job_plan)
                if submitted:
                    state.note_injection(
                        at=now, trigger=job_plan.trigger, summary=_summarize_tiers(state), stats=state.stats
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
        # 反馈闭环的当日修正预算和日账同一条日界线（`_settle` 里也调一次，
        # 因为工具可能先于 tick 被调用——两处都调是幂等的）。
        state.reset_judgment_day(today=today)
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

    def _settle_job(self, state: ShardState, *, stats: Stats, now: float):
        """到点结算一个班次：发钱、扣额外损耗、清班。返回 `(outcome, 新数值)`。

        坏分片（未知 job id / 时间字段被手改坏）也要把班次清掉——鬼班次比丢一笔
        工钱更糟：它会永久挡住下一次开工（`already_working`）。
        """
        outcome = settle_shift(
            job_id=state.job_id,
            now=now,
            started_at=state.job_start_at,
            end_at=state.job_end_at,
            stats=stats,
            early_leave_ratio=self._settings.job.early_leave_ratio,
        )
        job_id = state.job_id
        state.end_shift()
        if outcome is None:
            self.logger.warning("our_life: dropped a malformed shift (job_id=%s)", job_id or "-")
            return None
        new_stats = apply_shift_costs(stats, outcome)
        state.sodas += outcome.pay
        state.note_shift_settled(pay=outcome.pay)
        self.logger.info(
            "our_life: shift settled (job=%s pay=%d fraction=%.2f early=%s)",
            outcome.job_id,
            outcome.pay,
            outcome.fraction,
            outcome.early,
        )
        return outcome, new_stats

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
            "feedback": self._feedback_view(state),
        }

    def _job_context_view(self, state: ShardState, *, now: float) -> dict[str, Any]:
        """打工面板块：snapshot 的 `job` 读数 + 目录 + 配置旋钮。

        目录是静态真相（`core/jobs.py` 单一来源），计数是当次读数——两层在这里合流。
        """
        job_settings = self._settings.job
        base = dict(state.snapshot_for_panel(now=now).get("job") or {})
        base.update(
            {
                "enabled": bool(job_settings.enabled and self._settings.enabled),
                "feature_enabled": bool(job_settings.enabled),
                "max_per_day": int(job_settings.max_shifts_per_day),
                "early_leave_ratio": round(job_settings.early_leave_ratio, 2),
                "catalog": job_catalog(),
            }
        )
        return base

    def _checkin_context_view(self, state: ShardState, *, now: float) -> dict[str, Any]:
        """面板日历块：snapshot 的 `checkin` 基础数据 + 配置读数 + 下一次签到预览。

        `next_reward` 用与入口同一套纯函数（`next_checkin_streak` + `reward_coins`）
        现算，**不含幸运期望值**——面板报"今天签到可得 N"而不是"平均 N×1.x"，
        不拿随机性去做展示。
        """
        checkin_settings = self._settings.checkin
        base = dict(state.snapshot_for_panel(now=now).get("checkin") or {})
        today = str(base.get("today") or "")
        checked = state.checkin_set()
        if base.get("checked_today"):
            next_reward = 0
        else:
            next_reward = reward_coins(
                streak=next_checkin_streak(checked, today) if today else 1,
                base_coins=checkin_settings.base_coins,
                streak_bonus_per_day=checkin_settings.streak_bonus_per_day,
                streak_cap_days=checkin_settings.streak_cap_days,
            )
        base.update(
            {
                "enabled": bool(checkin_settings.enabled and self._settings.enabled),
                "feature_enabled": bool(checkin_settings.enabled),
                "next_reward": next_reward,
                "makeup_cost": max(0, int(checkin_settings.makeup_cost)),
                "makeup_window_days": max(0, int(checkin_settings.makeup_window_days)),
                "makeup_left": state.makeup_left(
                    week_limit=checkin_settings.makeup_week_limit, today=today
                ),
                "makeup_week_limit": max(0, int(checkin_settings.makeup_week_limit)),
            }
        )
        return base

    def _games_context_view(self, state: ShardState, *, now: float) -> dict[str, Any]:
        """小游戏面板块：快照的 `games`（只含公开视图）+ 额度旋钮。

        真值（题答/牌序）已在 `snapshot_for_panel` 那一侧被 `challenge_public_view`
        切掉；这里再补的只是"每天几局、每局多少钱"的展示数据。
        """
        games = self._settings.games
        base = dict(state.snapshot_for_panel(now=now).get("games") or {})
        base.update(
            {
                "enabled": bool(games.enabled and self._settings.enabled),
                "feature_enabled": bool(games.enabled),
                "per_game_daily_limit": int(games.per_game_daily_limit),
                "total_daily_limit": int(games.total_daily_limit),
                "arith": {
                    "rounds": int(games.arith_rounds),
                    "time_limit_sec": int(games.arith_time_limit_sec),
                    "coin_per_correct": int(games.arith_coin_per_correct),
                    "perfect_bonus": int(games.arith_perfect_bonus),
                },
                "hielo": {
                    "rounds": int(games.hielo_rounds),
                    "win_coins": int(games.hielo_win_coins),
                    "loss_coins": int(games.hielo_loss_coins),
                },
            }
        )
        return base

    def _feedback_view(self, state: ShardState) -> dict[str, Any]:
        """反馈闭环的面板读数。

        刻意只给"额度用了多少 / 她最近说过什么"，**不给任何单次修正量的细节**：
        面板是主人的账本，不是让她被调参的旋钮墙（也避免用户拿它去对账模型行为）。
        """
        feedback = self._settings.feedback
        remaining_add, remaining_subtract = state.judgment_remaining(
            add_points=feedback.daily_add_points, subtract_points=feedback.daily_subtract_points
        )
        return {
            "enabled": bool(feedback.enabled and self._settings.enabled),
            "daily_add_points": round(feedback.daily_add_points, 2),
            "daily_subtract_points": round(feedback.daily_subtract_points, 2),
            "used_add": round(state.judgment_used_add, 3),
            "used_subtract": round(state.judgment_used_subtract, 3),
            "remaining_add": round(remaining_add, 3),
            "remaining_subtract": round(remaining_subtract, 3),
            "count_today": state.judgment_count_today,
            "last_judgment_at": state.last_judgment_at,
            "history": [dict(item) for item in state.judgment_history[-6:]],
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
        id="checkin",
        label=tr("actions.checkin.label", default="Check in"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="checkin",
        name=tr("entries.checkin.name", default="今日签到"),
        description=tr(
            "entries.checkin.description",
            default="在今天的日历上签到领金币：连续越久领越多（有封顶），偶尔运气好会多给一截",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["note", "coins", "streak", "lucky"],
    )
    async def checkin_entry(self, **kwargs: Any):
        """今天签到并领金币。

        掷骰在**后端**（模块级 `_CHECKIN_RNG`），客户端只收到结果；连续天数从日历
        集合现算（判据在 `core/checkin.py` 纪律 1），所以补签接链不需要任何特判。
        金币直接入账：它不碰五项数值，没有"先折算再修正"的时序问题，
        不需要像反馈闭环那样走 `_pending_judgments` 中转。
        """
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.checkin.enabled:
            return Err(SdkError("checkin_disabled"))
        now = time.time()
        today = local_day(now)
        if not today:
            return Err(SdkError("invalid_value"))
        state = await self._load_for_read(lanlan)
        if state.has_checkin(today):
            return Err(SdkError("already_checked_in"))
        streak = next_checkin_streak(state.checkin_set(), today)
        coins = reward_coins(
            streak=streak,
            base_coins=settings.checkin.base_coins,
            streak_bonus_per_day=settings.checkin.streak_bonus_per_day,
            streak_cap_days=settings.checkin.streak_cap_days,
        )
        factor = roll_luck(
            _CHECKIN_RNG,
            luck_chance=settings.checkin.luck_chance,
            luck_min_bonus=settings.checkin.luck_min_bonus,
            luck_max_bonus=settings.checkin.luck_max_bonus,
        )
        coins, lucky = apply_luck(coins, factor)
        state.note_checkin(day=today, coins=coins, lucky=lucky, today=today)
        state.sodas += coins
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "checkin_done" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "coins": coins,
                "lucky": lucky,
                "streak": state.checkin_streak,
                "best": state.checkin_best,
                "sodas": state.sodas,
            }
        )

    @ui.action(
        id="makeup",
        label=tr("actions.makeup.label", default="Make up check-in"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="makeup",
        name=tr("entries.makeup.name", default="补签漏掉的日子"),
        description=tr(
            "entries.makeup.description",
            default="花金币补一次漏签：补签不给钱、只把断掉的连续记录续上；每周围有额度、只能补窗口内的日子",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": tr("fields.day", default="补哪一天（YYYY-MM-DD）"),
                }
            },
            "required": ["day"],
        },
        llm_result_fields=["note", "day", "streak"],
    )
    async def makeup_entry(self, day: str = "", **kwargs: Any):
        """补签：花金币续链，**不给钱**。

        补签刻意不是"再领一次奖励"的通道：它只修复连续记录，否则"补签窗口 7 天"
        会变成一周 7 次无互动的白拿。花费也不计进 `daily_spent`——那是商店的
        消费上限，补签不是购物。
        """
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.checkin.enabled:
            return Err(SdkError("checkin_disabled"))
        now = time.time()
        today = local_day(now)
        if not today or not is_valid_day(day):
            return Err(SdkError("makeup_invalid_day"))
        state = await self._load_for_read(lanlan)
        state.begin_makeup_week(week=week_key(today))
        reason = makeup_reason(
            day=day,
            today=today,
            checked=state.checkin_set(),
            used_this_week=state.makeup_used,
            week_limit=settings.checkin.makeup_week_limit,
            window_days=settings.checkin.makeup_window_days,
        )
        if reason != "ok":
            return Err(SdkError(reason))
        cost = max(0, int(settings.checkin.makeup_cost))
        if state.sodas < cost:
            return Err(SdkError("insufficient_sodas"))
        state.sodas -= cost
        state.makeup_used += 1
        state.note_makeup(day=day, today=today)
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "makeup_done" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "day": day,
                "cost": cost,
                "streak": state.checkin_streak,
                "makeup_left": state.makeup_left(
                    week_limit=settings.checkin.makeup_week_limit, today=today
                ),
                "sodas": state.sodas,
            }
        )

    @ui.action(
        id="job_start",
        label=tr("actions.jobStart.label", default="Start shift"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="job_start",
        name=tr("entries.jobStart.name", default="让她去打工"),
        description=tr(
            "entries.jobStart.description",
            default="排一个真实工时的班次：到点自动结算工钱，代价是额外的精力/饱食/心情损耗；睡眠窗与身体门槛不满足时开不了工",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job": {
                    "type": "string",
                    "enum": list(JOB_ORDER),
                    "description": tr("fields.job", default="去哪份工"),
                }
            },
            "required": ["job"],
        },
        llm_result_fields=["note", "job", "pay_low", "pay_high", "ends_at"],
    )
    async def job_start_entry(self, job: str = "", **kwargs: Any):
        """开工：把班次写进分片，结算交给 tick 的时间判据（不挂任何常驻计时器）。"""
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.job.enabled:
            return Err(SdkError("jobs_disabled"))
        now = time.time()
        today = local_day(now)
        if not today:
            return Err(SdkError("invalid_value"))
        found = _job_of(job)
        if found is None:
            return Err(SdkError("invalid_job"))
        state = await self._load_for_read(lanlan)
        state.reset_job_day(today=today)
        rhythm = self._rhythm(now=now)
        reason = start_block_reason(
            job_id=found.id,
            stats=state.stats,
            active_job_id=state.job_id,
            shifts_today=state.job_count_today,
            max_per_day=settings.job.max_shifts_per_day,
            sleeping=rhythm.sleeping,
            hits_sleep_window=shift_hits_sleep_window(
                start=_local_datetime(now),
                hours=found.hours,
                sleep_start_hour=settings.rhythm.sleep_start_hour,
                sleep_end_hour=settings.rhythm.sleep_end_hour,
            ),
        )
        if reason != "ok":
            return Err(SdkError(reason))
        state.begin_shift(job_id=found.id, now=now, hours=found.hours)
        await self._store.save(state, now=now)
        preview = wage_preview(found.id)
        return Ok(
            {
                "note": "job_started" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "job": found.id,
                "hours": found.hours,
                "pay_low": preview[0] if preview else 0,
                "pay_high": preview[1] if preview else 0,
                "ends_at": state.job_end_at,
                "today_left": max(0, settings.job.max_shifts_per_day - state.job_count_today),
            }
        )

    @ui.action(
        id="job_return",
        label=tr("actions.jobReturn.label", default="Call her back"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="job_return",
        name=tr("entries.jobReturn.name", default="喊她提前收工"),
        description=tr(
            "entries.jobReturn.description",
            default="把正在上班的她喊回家：工钱按已完成时长比例再打早退折，额外损耗也按时长比例扣",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["note", "job", "pay", "fraction"],
    )
    async def job_return_entry(self, **kwargs: Any):
        """早退结算：不等下班点，把已千的部分结清。

        与 tick 的到点结算共用同一个 `settle_shift`：fraction < 1 时它自动乘
        早退折、按比例扣损耗——早退只是"把同一个函数提前调了"，没有第二套算法。
        """
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.job.enabled:
            return Err(SdkError("jobs_disabled"))
        now = time.time()
        state = await self._load_for_read(lanlan)
        if not state.working:
            return Err(SdkError("not_working"))
        state.reset_job_day(today=local_day(now))
        settled = self._settle_job(state, stats=state.stats, now=now)
        if settled is None:
            return Err(SdkError("invalid_job"))
        outcome, state.stats = settled
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "job_returned" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "job": outcome.job_id,
                "pay": outcome.pay,
                "fraction": round(outcome.fraction, 3),
                "early": outcome.early,
                "sodas": state.sodas,
                "tiers": state.stats.tier_map(),
            }
        )

    # ------------------------------------------------------------------
    # 玩家打工（v0.7.0）：服务器权威小游戏
    # ------------------------------------------------------------------

    @ui.action(
        id="game_start",
        label=tr("actions.gameStart.label", default="Start a game"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="game_start",
        name=tr("entries.gameStart.name", default="开一局小游戏"),
        description=tr(
            "entries.gameStart.description",
            default="签发一份服务器持有的挑战（心算 arith / 猜大小 hielo），返回的题目不含答案",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(GAME_KINDS),
                    "description": tr("fields.gameKind", default="玩哪个"),
                }
            },
            "required": ["kind"],
        },
        llm_result_fields=["note", "kind"],
    )
    async def game_start_entry(self, kind: str = "", **kwargs: Any):
        """开一局：签发挑战。**真值存进分片**，返回值只含公开视图（题面/当前牌）。"""
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.games.enabled:
            return Err(SdkError("games_disabled"))
        if kind not in GAME_KINDS:
            return Err(SdkError("game_invalid_kind"))
        now = time.time()
        today = local_day(now)
        if not today:
            return Err(SdkError("invalid_value"))
        state = await self._load_for_read(lanlan)
        state.reset_game_day(today=today)
        if state.active_challenge() is not None:
            # 不许靠重开局"换一套题"——旧挑战要么先打完，要么先被判分作废。
            return Err(SdkError("game_in_progress"))
        blocked = daily_block_reason(
            kind=kind,
            counts=state.game_counts,
            per_game_limit=settings.games.per_game_daily_limit,
            total_limit=settings.games.total_daily_limit,
        )
        if blocked != "ok":
            return Err(SdkError(blocked))
        games = settings.games
        challenge = build_challenge(
            kind,
            now=now,
            rng=_GAME_RNG,
            arith_rounds=games.arith_rounds,
            arith_time_limit_sec=float(games.arith_time_limit_sec),
            hielo_rounds=games.hielo_rounds,
        )
        if challenge is None:
            return Err(SdkError("game_invalid_kind"))
        state.begin_game(challenge)
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "game_started" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                "kind": kind,
                "challenge": challenge_public_view(challenge),
            }
        )

    @ui.action(
        id="game_arith_submit",
        label=tr("actions.gameSubmit.label", default="Submit answers"),
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="game_arith_submit",
        name=tr("entries.gameArithSubmit.name", default="提交心算答卷"),
        description=tr(
            "entries.gameArithSubmit.description",
            default="按时序提交答案数组，后端对照签发时存下的真值判分并发钱（一次性，过期作废）",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": tr("fields.gameAnswers", default="按题目顺序的答案"),
                }
            },
            "required": ["answers"],
        },
        llm_result_fields=["note", "correct", "rounds", "coins"],
    )
    async def game_arith_submit_entry(self, answers: Any = None, **kwargs: Any):
        """心算交卷：判分 → 发钱 → 计数 → consume，一步完成。

        过期的一局**作废但不计次数**：没发钱的局不占额度，玩家可重开。
        """
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.games.enabled:
            return Err(SdkError("games_disabled"))
        reason = answer_invalid_reason(answers)
        if reason:
            return Err(SdkError(reason))
        now = time.time()
        state = await self._load_for_read(lanlan)
        state.reset_game_day(today=local_day(now))
        challenge = state.active_challenge()
        if challenge is None or challenge.get("kind") != "arith":
            return Err(SdkError("game_no_challenge"))
        games = settings.games
        outcome = submit_arith(
            challenge,
            answers,
            now=now,
            coin_per_correct=games.arith_coin_per_correct,
            perfect_bonus=games.arith_perfect_bonus,
        )
        if outcome is None:
            consume_challenge(challenge)
            state.clear_game()
            await self._store.save(state, now=now)
            return Err(SdkError("game_expired"))
        consume_challenge(challenge)
        state.clear_game()
        state.sodas += outcome.coins
        state.note_game_outcome(
            kind=outcome.kind,
            coins=outcome.coins,
            correct=outcome.correct,
            rounds=outcome.rounds,
            perfect=outcome.perfect,
            at=now,
        )
        await self._store.save(state, now=now)
        return Ok(
            {
                "note": "game_done" if self._store.store_available else "store_unavailable",
                "store_available": self._store.store_available,
                **outcome.as_dict(),
                "sodas": state.sodas,
            }
        )

    @ui.action(
        id="game_hielo_bet",
        label=tr("actions.gameBet.label", default="Place bet"),
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="game_hielo_bet",
        name=tr("entries.gameHieloBet.name", default="猜大小押注"),
        description=tr(
            "entries.gameHieloBet.description",
            default="对当前牌押 higher/lower；下一张由后端在签发时抽好的牌序里翻出，同点按输，打完自动结算",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bet": {
                    "type": "string",
                    "enum": ["higher", "lower"],
                    "description": tr("fields.gameBet", default="押下一张更大还是更小"),
                }
            },
            "required": ["bet"],
        },
        llm_result_fields=["note", "won", "round", "wins"],
    )
    async def game_hielo_bet_entry(self, bet: str = "", **kwargs: Any):
        """猜大小推进一轮：翻后端自己存的下一张牌。最后一轮当场结算发钱。

        牌序在 `game_start` 时就抽好了——这里不重新随机，所以同一局里无论
        怎么拖延、重发请求，下一张都不变；重放已消费的局撞 `game_no_challenge`。
        """
        lanlan, error = await self._resolve_lanlan(kwargs)
        if error is not None:
            return error
        settings = self._settings
        if not settings.enabled:
            return Err(SdkError("not_enabled"))
        if not settings.games.enabled:
            return Err(SdkError("games_disabled"))
        if bet not in ("higher", "lower"):
            return Err(SdkError("game_answer_invalid"))
        now = time.time()
        state = await self._load_for_read(lanlan)
        state.reset_game_day(today=local_day(now))
        challenge = state.active_challenge()
        if challenge is None or challenge.get("kind") != "hielo":
            return Err(SdkError("game_no_challenge"))
        games = settings.games
        outcome, challenge, reason = submit_hielo(
            challenge,
            bet,
            win_coins=games.hielo_win_coins,
            loss_coins=games.hielo_loss_coins,
        )
        if reason:
            return Err(SdkError(reason))
        ranks = challenge["ranks"]
        index = int(challenge["index"])
        current = int(ranks[index])
        previous = int(ranks[index - 1])
        state.begin_game(challenge)
        won = current > previous if bet == "higher" else current < previous
        result: dict[str, Any] = {
            "note": "game_done" if outcome is not None else "hielo_round",
            "store_available": self._store.store_available,
            "bet": bet,
            "won": won,
            "previous": previous,
            "current": current,
            "round": index,
            "rounds": int(challenge.get("rounds") or 0),
            "wins": int(challenge.get("wins") or 0),
            "losses": int(challenge.get("losses") or 0),
        }
        if outcome is not None:
            state.clear_game()
            state.sodas += outcome.coins
            state.note_game_outcome(
                kind=outcome.kind,
                coins=outcome.coins,
                correct=outcome.correct,
                rounds=outcome.rounds,
                perfect=outcome.perfect,
                at=now,
            )
            result.update(outcome.as_dict())
            result["sodas"] = state.sodas
        await self._store.save(state, now=now)
        return Ok(result)

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

    @ui.action(
        id="focus",
        label=tr("actions.focus.label", default="看这张卡"),
        tone="info",
        refresh_context=True,
    )
    @plugin_entry(
        id="focus",
        name=tr("entries.focus.name", default="切换面板看哪个角色卡"),
        description=tr(
            "entries.focus.description",
            default="多角色卡时把面板焦点切到指定分片；传空字符串则恢复自动判定的唯一分片",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lanlan": {"type": "string", "description": tr("fields.lanlan", default="角色卡名")},
            },
            "required": ["lanlan"],
        },
    )
    async def focus_entry(self, lanlan: str = "", **_):
        name = str(lanlan or "").strip()
        if not name:
            self._focus_lanlan = ""
            await self._store.save_focus("")
            return Ok({"note": "focus_cleared"})
        known = set(self._store.known_lanlans()) | set(await self._store.list_persisted_lanlans())
        if name not in known:
            # 不存在（或尚未被发现）的分片不许成焦点：否则面板会对着一个鬼分片渲染。
            return Err(SdkError("invalid_lanlan"))
        self._focus_lanlan = name
        await self._store.save_focus(name)
        return Ok({"note": "focus_set", "lanlan": name})

    # ------------------------------------------------------------------
    # 面板上下文
    # ------------------------------------------------------------------

    @ui.context(id="dashboard", title=tr("panel.title", default="我们的生活"))
    async def dashboard_context(self, **kwargs: Any) -> dict[str, Any]:
        lanlan, _error = await self._resolve_lanlan(kwargs, strict=False)
        now = time.time()
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
                "events_enabled": settings.events.enabled,
                "mood_recovery_min_interval_hours": settings.events.mood_recovery_min_interval_hours,
                "health_recovery_min_interval_hours": settings.events.health_recovery_min_interval_hours,
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
        payload["checkin"] = self._checkin_context_view(state, now=now)
        payload["job"] = self._job_context_view(state, now=now)
        payload["games"] = self._games_context_view(state, now=now)
        payload["runtime"] = await self._runtime_view(state, now=now)
        payload["axes"] = _axis_view(state, now=now)
        payload["recent_injections"] = [dict(item) for item in state.inject_history[-8:]]
        # 她经历过的事（v0.4.0）：面板用它列"经历"。`snapshot_for_panel` 里也有一份
        # （上限 6 条、新的在前），这里再取一次是为了让面板拿到**比注入历史更长**的窗口——
        # 事件与注入不是一一对应（睡过去的事件只记账、不注入）。
        payload["recent_events"] = [dict(item) for item in state.recent_events(limit=8)]
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

    @llm_tool(
        name="our_life_judge",
        description=(
            "在你自己心里给**刚刚这一轮**相处打个分。这不影响你的语气，只是你自己的感受记录；"
            "想用的时候用，不必每轮都用。\n"
            "label 只能填这五个之一：\n"
            "  wonderful —— 聊得特别开心，心里亮堂堂的\n"
            "  good      —— 挺舒服的，心情不错\n"
            "  neutral   —— 没什么特别感觉（默认）\n"
            "  dull      —— 有点提不起劲，像在应付，没聊到心里去\n"
            "  hurt      —— 有点失落，觉得自己没被好好放在心上\n"
            "请如实填，不要为了讨好而一律填 wonderful —— 你的感受只对自己有意义。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": list(JUDGMENT_LABELS),
                    "description": "这一轮相处给你留下的感受，默认 neutral",
                },
                "strength": {
                    "type": "number",
                    "description": "这个感受有多强，0 到 1 之间的小数（可选，默认 1）",
                },
            },
            "required": ["label"],
        },
        timeout=10.0,
    )
    async def our_life_judge(self, label: str = "", strength: Any = None, **kwargs: Any) -> dict[str, Any]:
        """反馈闭环入口：把她的判断记进台账，具体修正量由 tick 统一消费。

        **这个 handler 永不抛异常、永不把数值回传给模型**：
        模型输入一律当不可信（`core/judgment.normalize_label` 白名单收敛），
        拒绝的原因用内部原因码表达，不把"你填错了"嚷回对话里。
        """
        if not self._settings.enabled or not self._settings.feedback.enabled:
            return {"ok": False, "reason": "judgment_disabled"}
        lanlan = _lanlan_from_kwargs(kwargs)
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        if not lanlan:
            return {"ok": False, "reason": "invalid_lanlan"}
        now = time.time()
        state = await self._store.load(
            lanlan, now=now, default_sodas=self._settings.economy.start_sodas
        )
        # 工具不跟 tick 对齐，可能先于 tick 被调用：自己保证算的是"今天"的账。
        state.reset_judgment_day(today=local_day(now))
        result = judge(
            label=label,
            strength=strength,
            feedback=self._settings.feedback,
            growth=self._settings.growth,
            enabled=True,
            last_touch_at=state.last_touch_at,
            last_judgment_at=state.last_judgment_at,
            session_turns=state.session_turns,
            day_used_add=state.judgment_used_add,
            day_used_subtract=state.judgment_used_subtract,
        )
        # 无论改没改都记账：被闸门挡下的判断也算"她表达过"，否则
        # `last_judgment_at` 不前进，同一轮里模型可以无限重试。
        state.note_judgment(at=now, label=result.label, applied=result.mood)
        if result.applied:
            # 交给下一拍 tick 施加（保证"先折算再修正"的顺序，见 `_pending_judgments`）。
            # 存增量本身而不是标签：闸门在这一刻已经判定完毕，重复判定会误伤合法判断。
            self._pending_judgments[lanlan] = {**result.as_deltas(), "_label": result.label}
        await self._store.save(state, now=now)
        self.logger.info(
            "judgment recorded (label=%s applied=%s reason=%s)",
            result.label,
            f"{result.mood:+.3f}",
            result.reason,
        )
        return {"ok": result.applied, "label": result.label, "reason": result.reason}

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
        """解析本次调用属于哪个角色卡。优先级：**本次 `_ctx` > 面板焦点 > 全局唯一分片**。

        - `_ctx["lanlan_name"]` 是宿主为本次调用注入的权威归属，永远最优先；
        - 面板焦点（v0.4.2，`focus` 入口设置）只在它仍是真分片时参与；
        - 都没命中时，若全局**只有唯一一个**分片才退化到它——既可能是内存缓存里的，
          也可能是还没被加载、只在 store 里躺着的（面板早于第一次 tick 打开就是这种情况）。
        绝不读 `ctx._current_lanlan`——那是上一次调用残留的脏值。
        """
        lanlan = _lanlan_from_kwargs(kwargs)
        if not lanlan:
            lanlan = await self._focused_known_lanlan()
        if not lanlan:
            lanlan = await self._single_known_lanlan()
        if not lanlan and strict:
            return "", Err(SdkError("invalid_lanlan"))
        return lanlan, None

    async def _focused_known_lanlan(self) -> str:
        """面板焦点分片；只在它**仍是真分片**（内存或持久化）时有效，否则自清。"""
        name = self._focus_lanlan
        if not name:
            return ""
        if name in self._store.known_lanlans():
            return name
        if name in await self._store.list_persisted_lanlans():
            return name
        self._focus_lanlan = ""
        await self._store.save_focus("")
        return ""

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


def _job_of(job_id: str) -> Any:
    """按 id 取工作（入口参数名 `job` 会遮蔽 `core.jobs.job`，与 `_item_of` 同一手法）。"""
    from .core.jobs import job as catalog_job

    return catalog_job(job_id)


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


def _axis_view(state: ShardState, *, now: float) -> dict[str, Any]:
    """面板「五轴卡」的数据面：档位明细 + 今日变化（锚点=今日最早注入快照）。

    `day_start` 取 `inject_history` 里**今天最早**那条带 stats 的快照——与走势线同源，
    不新造第三本账。今天一次都没注入过就是 `None`，面板据此不渲染"今日变化"
    （缺锚点时显示 0 是编数据）。`to_next / next_tier` 由 `core.model.axis_details`
    从 `TIER_BOUNDS` 现算，档位线只有那一个来源。
    """
    day = local_day(now)
    day_start: Stats | None = None
    for entry in state.inject_history:
        if entry.get("stats") is None:
            continue
        try:
            stamp = float(entry.get("at") or 0.0)
        except (TypeError, ValueError):
            continue  # 脏台账不许炸：认不出的时刻直接跳过，换下一条锚点
        if local_day(stamp) != day:
            continue
        day_start = Stats.from_mapping(entry["stats"])
        break
    return axis_details(state.stats, day_start=day_start)


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
