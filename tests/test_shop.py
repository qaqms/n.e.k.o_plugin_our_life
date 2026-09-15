"""商店深化（v0.7.0 #4）：解锁锁存、当日限定、每日特惠与收藏件产出的常驻门。

盯四条纪律（`core/shop.py` 模块 docstring 的取舍段）：

1. **解锁判据只读已有计数器**——唯一的新状态是锁存账 `shop_unlocks`（schema 6），
   且写入前有 `normalize_unlocks` 硬消毒。
2. **幸运符永不进锁存账**——"仅幸运签当日、过期下架"是行为契约，不是展示巧合。
3. **可见性与成交价都在后端复算**——面板参数可任意伪造（与小游戏同一威胁模型），
   未上架的货必须撞 `shop_locked`，特惠日必须按后端哈希收钱。
4. **收藏件是"拥有"不是"用掉"**——`feed` 的硬门与背包的不渲染按钮是两层，
   后端门是真相。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from our_life.core.configuration import OurLifeSettings
from our_life.core.economy import Inventory, meal_plan, recharge_plan
from our_life.core.shop import (
    GATED_PERMANENT,
    PUDDING_MIN_STREAK,
    ROYAL_MIN_SHIFTS,
    YIELD_BACKFILL_CAP,
    UnlockFacts,
    condition_met,
    daily_deals,
    discounted_cost,
    fresh_unlocks,
    keepsake_daily,
    keepsake_yield_days,
    normalize_unlocks,
    unit_cost,
    visible_ids,
)
from our_life.services.state import ShardState

SHARD_KEY = "ourlife@灵"
CTX = {"_ctx": {"lanlan_name": "灵"}}
NOW = 1_800_000_000.0  # 与 test_entries 同一个钉死钟点（真机时钟会漂，门不许漂）


def _today(ts: float = NOW) -> str:
    from our_life.core.behavior import local_day

    return local_day(ts)


def _facts(
    *,
    shifts: int = 0,
    best: int = 0,
    close: bool = False,
    lucky: bool = False,
) -> UnlockFacts:
    return UnlockFacts(
        job_shifts_total=shifts,
        checkin_best=best,
        affection_close=close,
        lucky_today=lucky,
    )


def _shop_config(**economy_overrides: Any) -> dict[str, Any]:
    economy: dict[str, Any] = {"enabled": True}
    economy.update(economy_overrides)
    return {"our_life": {"enabled": True, "economy": economy}}


def _seed_shard(
    host: Any,
    *,
    sodas: int = 500,
    affection: float = 30.0,
    mood: float = 60.0,
    inventory: dict[str, int] | None = None,
    account_date: str | None = None,
    shop_unlocked: list[str] | None = None,
    checkin_log: list[Any] | None = None,
    job_shifts_total: int = 0,
    checkin_best: int = 0,
    daily_allowance_granted: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 6,
        "stats": {
            "affection": affection,
            "mood": mood,
            "health": 70.0,
            "satiety": 70.0,
            "energy": 80.0,
        },
        "last_decay_at": NOW,  # 零流逝 = 零衰减：产出断言不被衰减噪声污染
        "sodas": sodas,
        "inventory": dict(inventory or {}),
        "account_date": _today() if account_date is None else account_date,
        "daily_allowance_granted": daily_allowance_granted,
        "daily_spent": 0,
        "shop_unlocked": list(shop_unlocked or []),
        "checkin_log": list(checkin_log or []),
        "job_shifts_total": job_shifts_total,
        "checkin_best": checkin_best,
    }
    host.store.data[SHARD_KEY] = payload
    return payload


# ---------------------------------------------------------------------------
# 纯函数：解锁判据与可见性
# ---------------------------------------------------------------------------


def test_unlock_conditions_are_boundary_exact() -> None:
    assert not condition_met("royal", _facts(shifts=ROYAL_MIN_SHIFTS - 1))
    assert condition_met("royal", _facts(shifts=ROYAL_MIN_SHIFTS))
    assert not condition_met("pudding", _facts(best=PUDDING_MIN_STREAK - 1))
    assert condition_met("pudding", _facts(best=PUDDING_MIN_STREAK))
    assert not condition_met("giftbox", _facts(close=False))
    assert condition_met("giftbox", _facts(close=True))
    assert not condition_met("charm", _facts(lucky=False))
    assert condition_met("charm", _facts(lucky=True))
    # 常驻货没有判据：谁来了都在架
    assert condition_met("meat", _facts())


def test_locked_items_are_invisible_until_unlocked() -> None:
    base = set(visible_ids(_facts(), ()))
    assert "meat" in base and "gift" in base
    for gated in (*GATED_PERMANENT, "charm"):
        assert gated not in base, f"{gated} must be hidden before its line"
    assert "royal" in visible_ids(_facts(shifts=ROYAL_MIN_SHIFTS), ())
    assert "giftbox" in visible_ids(_facts(close=True), ())
    assert "charm" in visible_ids(_facts(lucky=True), ())


def test_latched_giftbox_survives_the_affection_drop() -> None:
    """锁存账的**存在理由**：好感掉下亲近档，礼盒也不许从货架上消失。"""
    pool = visible_ids(_facts(close=False), ("giftbox",))
    assert "giftbox" in pool


def test_charm_is_never_latched_even_if_the_ledger_is_dirty() -> None:
    """昨日幸运符**不该**因为任何锁存痕迹今天还在架——当日限定是硬契约。"""
    assert "charm" not in visible_ids(_facts(lucky=False), normalize_unlocks(["charm"]))
    # 写侧消毒：非法 id 与 charm 都进不了账
    assert normalize_unlocks(["charm", "royal", "royal", "gold_apple", "", 7]) == ("royal",)
    assert normalize_unlocks(None) == ()
    assert normalize_unlocks({"royal": True}) == ()


def test_fresh_unlocks_only_reports_permanent_and_unseen() -> None:
    facts = _facts(shifts=99, best=99, close=True, lucky=True)
    fresh = fresh_unlocks(facts, ("royal",))
    assert "charm" not in fresh, "幸运符永远不该出现在锁存候选里"
    assert set(fresh) == {"pudding", "giftbox"}


# ---------------------------------------------------------------------------
# 纯函数：每日特惠
# ---------------------------------------------------------------------------


def test_deals_are_deterministic_bounded_and_within_pool() -> None:
    pool = visible_ids(_facts(), ())
    first = daily_deals("2026-09-15", "灵", pool)
    second = daily_deals("2026-09-15", "灵", pool)
    assert first == second, "同一 (日期, 卡名, 池) 必须给出同一份今日价（面板轮询/入口复算共用）"
    assert 1 <= len(first) <= 2
    assert set(first) <= set(pool)
    assert set(first.values()) == {30}
    assert daily_deals("2026-09-15", "灵", []) == {}
    assert daily_deals("not-a-day!!", "灵", pool)  # 日期串由后端生成，坏了也不许崩


def test_deals_actually_vary_across_days_or_names() -> None:
    """反摆烂门：哈希若退化成常数，上面所有测试仍绿——这里逼它真的会变。"""
    pool = visible_ids(_facts(), ())
    seen = {tuple(sorted(daily_deals(f"2026-09-{day:02d}", "灵", pool))) for day in range(1, 29)}
    assert len(seen) > 1


def test_discounted_cost_rounds_with_a_floor_of_one() -> None:
    assert discounted_cost("meat", 30) == 4  # 6×0.7=4.2 → 4
    assert discounted_cost("fish", 30) == 7
    assert discounted_cost("gift", 30) == 21
    assert discounted_cost("meat", 100) == 1, "白送会击穿日消费限额的意义"
    assert discounted_cost("gold_apple", 30) == 0, "未知物品没有价目"


def test_unit_cost_is_the_single_price_authority() -> None:
    day = "2026-09-15"
    pool = visible_ids(_facts(), ())
    deals = daily_deals(day, "灵", pool)
    for item_id in pool:
        expected = discounted_cost(item_id, 30) if item_id in deals else 0
        got = unit_cost(day, "灵", pool, item_id)
        if item_id in deals:
            assert got == expected
        else:
            from our_life.core.economy import item

            assert got == item(item_id).cost_sodas


# ---------------------------------------------------------------------------
# 纯函数：收藏件产出
# ---------------------------------------------------------------------------


def test_keepsake_daily_sums_owned_pieces_only() -> None:
    inv = Inventory.from_mapping({"charm": 1, "giftbox": 1, "meat": 40})
    assert keepsake_daily(inv) == {"coins": 3.0, "mood": 2.0}
    assert keepsake_daily(Inventory.from_mapping({"meat": 4})) == {}


def test_keepsake_yield_days_backfills_with_a_cap() -> None:
    assert keepsake_yield_days("2026-09-15", "2026-09-15") == 0, "当天买入当天不产"
    assert keepsake_yield_days("", "2026-09-15") == 0, "没有上次日界线就无从补起"
    assert keepsake_yield_days("2026-09-12", "2026-09-15") == 3
    assert keepsake_yield_days("2026-08-15", "2026-09-15") == YIELD_BACKFILL_CAP
    assert keepsake_yield_days("2026-09-16", "2026-09-15") == 0, "时光倒流的脏数据发 0 份"
    assert keepsake_yield_days("nonsense", "2026-09-15") == 0


def test_new_foods_respect_the_staple_preference_discipline() -> None:
    """皇家肉干**可选**口粮但不自动升级：不设为 staple 时，自动进食仍先吃便宜的。"""
    inv = Inventory.from_mapping({"royal": 2, "meat": 2})
    assert meal_plan(inv, staple_item_id="meat", satiety=10.0) == "meat"
    assert meal_plan(inv, staple_item_id="royal", satiety=10.0) == "royal"
    # 背包里只剩贵粮时也该吃（饿 > 省钱）
    assert meal_plan(Inventory.from_mapping({"royal": 1}), staple_item_id="meat", satiety=10.0) == "royal"


def test_recharge_plan_honours_the_backend_unit_cost() -> None:
    plan = recharge_plan(
        item_id="meat", quantity=2, inventory=None, coins=100, unit_cost=4
    )
    assert plan.ok and plan.unit_cost == 4 and plan.total_cost == 8
    broke = recharge_plan(item_id="meat", quantity=2, inventory=None, coins=7, unit_cost=4)
    assert not broke.ok and broke.reason == "insufficient_sodas"


# ---------------------------------------------------------------------------
# 入口行为（后端复算面）
# ---------------------------------------------------------------------------


def test_shop_rejects_locked_items_with_a_stable_code(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host)
    for locked in ("royal", "pudding", "giftbox", "charm"):
        result = run_async(plugin.shop_entry(item=locked, quantity=1, **CTX))
        assert not result.is_ok()
        assert str(result.error) == "shop_locked", f"{locked} must not be buyable before its line"


def test_shop_unlocks_luckys_charm_only_on_a_lucky_day(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """幸运符的当日窗：判据是**今天**的幸运签，昨天的不算。"""
    monkeypatch.setattr(time, "time", lambda: NOW)
    # 昨日幸运 ≠ 今日有货（独立 plugin：StateStore 内存缓存不重读 store，这是夹具语义）。
    stale, stale_host = make_plugin(config=_shop_config())
    stale._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(stale_host, checkin_log=[["2020-01-01", 12, 1]])
    blocked = run_async(stale.shop_entry(item="charm", quantity=1, **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "shop_locked"

    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, checkin_log=[[_today(), 18, 1]])
    result = run_async(plugin.shop_entry(item="charm", quantity=1, **CTX))
    assert result.is_ok()
    assert result.value["inventory"] == {"charm": 1}
    # 幸运符不进锁存账：明日复算（换个"今天"）它就又下架了
    assert "charm" not in host.store.data[SHARD_KEY]["shop_unlocked"]


def test_shop_gates_charge_at_the_backend_price_not_the_displayed_one(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """特惠日成交必须按后端哈希收钱：`unit`/`deal`/余额/日消费四账同源。"""
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host)
    pool = visible_ids(_facts(), ())
    day = _today()
    # 挑一个今天 meat 在折扣名单上的"角色卡名"（哈希对名字敏感——这正是要钉的复算面）
    deals = daily_deals(day, "灵", pool)
    target = "灵"
    probe = 0
    while "meat" not in deals and probe < 100:
        probe += 1
        target = f"特惠探针{probe}"
        deals = daily_deals(day, target, pool)
    assert "meat" in deals, "100 次探针必中；不中说明哈希退化成了与名字无关"
    host.store.data[f"ourlife@{target}"] = dict(
        host.store.data[SHARD_KEY], account_date=day, daily_allowance_granted=True, daily_spent=0
    )
    result = run_async(plugin.shop_entry(item="meat", quantity=3, _ctx={"lanlan_name": target}))
    assert result.is_ok()
    expected_unit = discounted_cost("meat", deals["meat"])
    assert result.value["unit"] == expected_unit
    assert result.value["deal"] is True
    assert result.value["cost"] == expected_unit * 3
    saved = host.store.data[f"ourlife@{target}"]
    assert saved["sodas"] == 500 - expected_unit * 3
    assert saved["daily_spent"] == expected_unit * 3, "限额必须按**实付**计，不是标价"


def test_purchase_latches_the_unlock_forever(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, affection=95.0)
    bought = run_async(plugin.shop_entry(item="giftbox", quantity=1, **CTX))
    assert bought.is_ok()
    assert "giftbox" in host.store.data[SHARD_KEY]["shop_unlocked"]
    # 好感崩到陌生人：卡还在架上（买第二件撞 carry_full 而不是 shop_locked）
    host.store.data[SHARD_KEY]["stats"] = dict(
        host.store.data[SHARD_KEY]["stats"], affection=2.0
    )
    again = run_async(plugin.shop_entry(item="giftbox", quantity=1, **CTX))
    assert not again.is_ok()
    assert str(again.error) == "carry_full", "once unlocked, never re-locked"
    pool = [str(entry["id"]) for entry in run_async(plugin.dashboard_context(**CTX))["shop"]]
    assert "giftbox" in pool


def test_feed_refuses_to_consume_a_keepsake(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, inventory={"charm": 1})
    result = run_async(plugin.feed_entry(item="charm", **CTX))
    assert not result.is_ok()
    assert str(result.error) == "item_keepsake"
    assert host.store.data[SHARD_KEY]["inventory"] == {"charm": 1}, "被拒的入口不许动背包"


def test_dashboard_hides_locked_shelf_and_publishes_backend_prices(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, job_shifts_total=ROYAL_MIN_SHIFTS)
    shelf = run_async(plugin.dashboard_context(**CTX))["shop"]
    ids = [str(entry["id"]) for entry in shelf]
    assert "royal" in ids, "判据达线即上架（锁存只是不回退，不是上架的唯一通道）"
    assert "pudding" not in ids and "charm" not in ids
    for entry in shelf:
        assert entry["price"] == unit_cost(_today(), "灵", tuple(ids), str(entry["id"]))
        assert entry["rarity"] in ("common", "uncommon", "rare")
        assert isinstance(entry["keepsake"], bool)
    # 零分片（连兜底候选都没有）才走 invalid_lanlan：货架空而不是泄底全表。
    fresh, _fresh_host = make_plugin(config=_shop_config())
    fresh._settings = OurLifeSettings.from_config(_shop_config())
    without = run_async(fresh.dashboard_context())
    assert without["error_code"] == "invalid_lanlan"
    assert without["shop"] == []


def test_tick_grants_keepsake_income_with_the_backfill_cap(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    plugin._last_tick_at = NOW  # 与 test_entries 同一手法：跳过节流，直调结算
    _seed_shard(
        host,
        inventory={"charm": 1, "giftbox": 1},
        account_date="1999-01-01",  # 极端缺席：必须被封顶成 7 份而不是天荒地老
        mood=40.0,
    )
    run_async(plugin._settle("灵", (), now=NOW))
    saved = host.store.data[SHARD_KEY]
    # 跨天日界线也会**同时**重发今日零花钱（与收藏件补发同一条日界线，两本账各自记账）。
    allowance = plugin._settings.economy.daily_allowance
    assert saved["sodas"] == 500 + allowance + 3 * YIELD_BACKFILL_CAP
    assert saved["stats"]["mood"] == pytest.approx(40.0 + 2 * YIELD_BACKFILL_CAP)
    assert saved["inventory"] == {"charm": 1, "giftbox": 1}, "产出是'拥有'的红利，不是消耗"


def test_tick_does_not_grant_income_on_the_purchase_day(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """买入当天不产：产出从次日日界线起算（`keepsake_yield_days` 同日返回 0）。"""
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, inventory={"charm": 1})  # account_date = 今天
    run_async(plugin._settle("灵", (), now=NOW))
    assert host.store.data[SHARD_KEY]["sodas"] == 500


def test_tick_latches_the_ledger_alongside_the_day_line(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    _seed_shard(host, job_shifts_total=ROYAL_MIN_SHIFTS, checkin_best=PUDDING_MIN_STREAK, affection=30.0)
    run_async(plugin._settle("灵", (), now=NOW))
    assert host.store.data[SHARD_KEY]["shop_unlocked"] == ["pudding", "royal"]
    assert "giftbox" not in host.store.data[SHARD_KEY]["shop_unlocked"]


def test_schema_v5_shards_read_with_an_empty_ledger(
    make_plugin: Any, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧分片（schema 5）没有锁存账：缺键=空账，不迁移、不报错、不泄底。"""
    monkeypatch.setattr(time, "time", lambda: NOW)
    plugin, host = make_plugin(config=_shop_config())
    plugin._settings = OurLifeSettings.from_config(_shop_config())
    payload = _seed_shard(host)
    payload.pop("shop_unlocked")
    payload["schema_version"] = 5
    shelf = run_async(plugin.dashboard_context(**CTX))["shop"]
    assert "giftbox" not in [str(entry["id"]) for entry in shelf]
    state = ShardState.from_payload("灵", payload, now=NOW)
    assert state.shop_unlocked == ()
