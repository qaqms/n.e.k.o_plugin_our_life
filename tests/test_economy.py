"""经济层门：物品表、背包、口粮消耗、购买规则、日薪、口粮顾问。

这一层的"数值"直接变成用户看到的面板建议（"还够几天、要买几份"），所以断言写在
**语义层**：她能吃几天、钱够不够、背包装不装得下——而不是复述乘除法。
"""

from __future__ import annotations

import pytest
from our_life.core.economy import (
    ITEM_IDS,
    ITEM_ORDER,
    ITEMS,
    MEAL_RESTORE,
    Inventory,
    advise,
    bump_meal_day,
    daily_income,
    effects_for,
    item,
    meal_need_per_day,
    meal_plan,
    meal_restore,
    observed_meals_per_day,
    recharge_plan,
    stock_days,
    stock_meals,
    total_units,
)

# ---------------------------------------------------------------------------
# 物品表与背包
# ---------------------------------------------------------------------------


def test_every_catalog_item_is_addressable_and_ordered() -> None:
    assert ITEM_IDS == ITEM_ORDER, "id 顺序必须与展示顺序一致（面板与 i18n 按它渲染）"
    assert len(set(ITEM_IDS)) == len(ITEM_IDS)
    for item_id in ITEM_IDS:
        found = item(item_id)
        assert found is not None
        assert found.id == item_id
        assert found.cost_sodas > 0
        # v0.7.0："买了总得有什么用"有两种形态——即时效果（消耗品）或每日产出（收藏件）。
        assert found.effects or found.daily, f"{item_id} has neither effects nor daily yield"


def test_only_food_items_can_be_eaten() -> None:
    edible = {entry.id for entry in ITEMS.values() if entry.food}
    assert edible, "至少要有一件能当饭吃的东西"
    for item_id in edible:
        assert effects_for(item_id).get("satiety") == pytest.approx(meal_restore(item_id)), (
            f"{item_id} 的饱食效果与 meal_restore 不一致（会造成面板建议与实际效果漂移）"
        )


def test_default_meal_restore_is_used_for_unknown_items() -> None:
    assert meal_restore("no_such_item") == MEAL_RESTORE


def test_inventory_ignores_unknown_items_and_bad_counts() -> None:
    inv = Inventory.from_mapping({"meat": 3, "not_an_item": 99, "fish": 0, "cake": -2, "toy": "x"})
    assert inv.as_dict() == {"meat": 3}
    # 未知物品不保留：物品表是单一来源，留着只会在面板上显示成空名字
    assert inv.get("not_an_item") == 0


def test_inventory_add_and_consume_are_clamped() -> None:
    inv = Inventory().with_added("meat", 2)
    assert inv.get("meat") == 2
    assert inv.with_consumed("meat", 1).get("meat") == 1
    assert inv.with_consumed("meat", 5).get("meat") == 0  # 不会变成负数
    assert Inventory().with_added("unknown", 5).as_dict() == {}


def test_only_food_counts_toward_stock() -> None:
    inv = Inventory.from_mapping({"meat": 2, "fish": 1, "medicine": 5, "toy": 1})
    assert stock_meals(inv) == 3
    assert total_units(inv) == 9


# ---------------------------------------------------------------------------
# 吃饭
# ---------------------------------------------------------------------------


def test_she_eats_only_when_hungry() -> None:
    stock = Inventory.from_mapping({"meat": 2})
    assert meal_plan(stock, staple_item_id="meat", satiety=90.0, threshold=55.0) is None
    assert meal_plan(stock, staple_item_id="meat", satiety=54.9, threshold=55.0) == "meat"


def test_she_eats_nothing_when_the_bag_is_empty() -> None:
    assert meal_plan(Inventory(), staple_item_id="meat", satiety=10.0, threshold=55.0) is None
    # 只有不能吃的东西也等于没粮
    assert meal_plan(Inventory.from_mapping({"medicine": 3}), staple_item_id="meat", satiety=10.0, threshold=55.0) is None


def test_staple_is_preferred_over_other_food() -> None:
    stock = Inventory.from_mapping({"meat": 1, "fish": 5})
    assert meal_plan(stock, staple_item_id="meat", satiety=10.0, threshold=55.0) == "meat"


def test_missing_staple_falls_back_to_any_food() -> None:
    """主人没囤指定口粮时，她吃背包里别的能吃的东西，而不是干饿着。"""
    stock = Inventory.from_mapping({"fish": 1, "medicine": 2})
    assert meal_plan(stock, staple_item_id="meat", satiety=10.0, threshold=55.0) == "fish"


# ---------------------------------------------------------------------------
# 购买规则
# ---------------------------------------------------------------------------


def test_purchase_succeeds_when_affordable_and_carriable() -> None:
    plan = recharge_plan(item_id="meat", quantity=3, inventory=Inventory(), coins=100)
    assert plan.ok
    assert plan.total_cost == ITEMS["meat"].cost_sodas * 3
    assert plan.carry_left == ITEMS["meat"].carry_max or ITEMS["meat"].carry_max == 0


def test_purchase_reason_codes_are_stable() -> None:
    assert recharge_plan(item_id="nope", quantity=1, inventory=Inventory(), coins=999).reason == "unknown_item"
    assert recharge_plan(item_id="meat", quantity=0, inventory=Inventory(), coins=999).reason == "invalid_quantity"
    assert recharge_plan(item_id="meat", quantity=-2, inventory=Inventory(), coins=999).reason == "invalid_quantity"
    assert recharge_plan(item_id="meat", quantity=2, inventory=Inventory(), coins=1).reason == "insufficient_sodas"
    limited = recharge_plan(
        item_id="meat", quantity=2, inventory=Inventory(), coins=999, daily_spent=240, daily_limit=240
    )
    assert limited.reason == "over_daily_limit"


def test_purchase_respects_the_carry_cap() -> None:
    medicine = ITEMS["medicine"]
    assert medicine.carry_max > 0
    full = recharge_plan(
        item_id="medicine",
        quantity=1,
        inventory=Inventory.from_mapping({"medicine": medicine.carry_max}),
        coins=999,
    )
    assert not full.ok
    assert full.reason == "carry_full"
    assert full.carry_left == 0


def test_free_purchase_still_needs_the_item() -> None:
    """零消费上限（0 = 不限）不等于零校验：物品与数量照样要合法。"""
    assert recharge_plan(item_id="meat", quantity=1, inventory=Inventory(), coins=0, daily_limit=0).ok is False


# ---------------------------------------------------------------------------
# 日薪
# ---------------------------------------------------------------------------


def test_daily_income_is_bounded_per_session() -> None:
    """想多挣只能多天连续相处，不能在一天里刷爆经济。"""
    cap = 20
    greedy = daily_income(turns=500, is_new_day=False, streak_days=1, max_per_session=cap)
    bounded = daily_income(turns=cap, is_new_day=False, streak_days=1, max_per_session=cap)
    assert greedy == bounded


def test_daily_income_rewards_coming_back_and_streaks() -> None:
    plain = daily_income(turns=1, is_new_day=False, streak_days=1)
    new_day = daily_income(turns=1, is_new_day=True, streak_days=1)
    long_streak = daily_income(turns=1, is_new_day=True, streak_days=30)
    assert new_day > plain
    assert long_streak >= new_day


def test_daily_income_is_zero_without_interaction_or_new_day() -> None:
    assert daily_income(turns=0, is_new_day=False, streak_days=5) == 0


# ---------------------------------------------------------------------------
# 口粮顾问
# ---------------------------------------------------------------------------


def test_meal_need_per_day_derives_from_decay_and_restore() -> None:
    need = meal_need_per_day(per_day_decay=76.0, restore_per_meal=38.0, threshold=55.0)
    # 消耗 76 分/天 + 45 分缓冲，一餐补 38 → 约 3.2 餐/天
    assert need == pytest.approx((76.0 + 45.0) / 38.0, rel=1e-6)
    assert meal_need_per_day(per_day_decay=0.0) >= 1.0, "至少也要按一餐算，不能给 0"


def test_observed_meal_rate_uses_days_with_records() -> None:
    days = (("2026-09-10", 3), ("2026-09-11", 2), ("2026-09-12", 2))
    # 分母是有记录的天数（3），不是窗口长度（7）——否则"一天吃 3 餐"会被摊成 0.43
    assert observed_meals_per_day(days, window_days=7) == pytest.approx(7 / 3)
    assert observed_meals_per_day((), window_days=7) == 0.0


def test_observed_meal_rate_only_looks_at_the_window() -> None:
    days = tuple((f"2026-09-{day:02d}", 1) for day in range(1, 11))
    assert observed_meals_per_day(days, window_days=3) == pytest.approx(1.0)


def test_meal_day_ledger_stays_bounded() -> None:
    ledger: tuple[tuple[str, int], ...] = ()
    for day in range(1, 21):
        ledger = bump_meal_day(ledger, f"2026-09-{day:02d}", keep=14)
    assert len(ledger) == 14
    assert ledger[-1][0] == "2026-09-20"


def test_advisor_reports_days_remaining_and_shortfall() -> None:
    advisor = advise(
        inventory=Inventory.from_mapping({"meat": 6}),
        coins=100,
        meal_need=2.0,
        observed_meals_per_day=2.5,
        horizon_days=7,
        warn_days=2.0,
    )
    # 取实测与理论里更大的那个（保守：宁可多囤）
    assert advisor.meals_per_day == pytest.approx(2.5)
    assert advisor.stock_meals == 6
    assert advisor.days_remaining == pytest.approx(2.4)
    assert advisor.shortfall_meals == 18 - 6  # ceil(2.5*7)=18
    assert advisor.suggested_purchase == 12
    assert advisor.suggested_cost == 12 * ITEMS["meat"].cost_sodas


def test_advisor_flags_urgent_and_empty() -> None:
    empty = advise(inventory=Inventory(), coins=0, meal_need=3.0, horizon_days=7, warn_days=2.0)
    assert empty.empty and empty.urgent
    assert not empty.affordable
    stocked = advise(
        inventory=Inventory.from_mapping({"meat": 30}), coins=0, meal_need=2.0, horizon_days=7, warn_days=2.0
    )
    assert not stocked.urgent
    assert not stocked.empty


def test_advisor_is_not_urgent_merely_because_it_cannot_afford_a_restock() -> None:
    """钱不够但粮充足 = 还没到紧急；真正紧急是"快没粮"或"没粮且补不起"。"""
    advisor = advise(
        inventory=Inventory.from_mapping({"meat": 20}),
        coins=0,
        meal_need=2.0,
        horizon_days=7,
        warn_days=2.0,
    )
    # 囤到 20 份已经超过 7 天的视野，所以"要补 0 份"——不需要花钱就无所谓买不买得起
    assert advisor.suggested_purchase == 0
    assert advisor.suggested_cost == 0
    assert advisor.affordable
    assert not advisor.urgent


def test_stock_days_handles_a_zero_need() -> None:
    assert stock_days(5, per_day=0.0) == float("inf")
    assert stock_days(5, per_day=2.0) == pytest.approx(2.5)
    assert stock_days(0, per_day=2.0) == 0.0
