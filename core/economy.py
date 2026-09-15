"""经济层：物品表、背包账本、口粮消耗与"口粮顾问"——纯函数，零 SDK 依赖。

v0.7.0 起，"谁能上架、今天卖什么价、收藏件每天产出多少"这套货架规则在
`core/shop.py`；本模块只拥有**物品本体**（表、效果、背包、成交可行性）。

这一层回答三个问题（v0.2.0「过日子」主线）：

1. **她每天要吃多少**：`meal_need_per_day` —— 由饱食衰减速率、一餐补多少、
   以及她的作息窗共同推出，不是拍脑袋的常数。面板的"口粮顾问"就是把这个数
   摊开给主人看：`avg_meals_per_day` / `inventory_meals` / `days_remaining` / 缺口。
2. **买东西的规则**：`recharge_plan` —— 金币够不够、身上带得动带不动（`carry_max`）。
3. **每天吃什么**：`meal_plan` —— 背包里按"食物类优先、便宜的先用"自动吃。

设计取舍（都写进 docstring，避免后人当成疏漏）：

- **物品表是枚举单一来源**，`plugin.toml` 只暴露**行为旋钮**
  （`staple_item_id` 口粮种类、`daily_allowance` 零花钱、`carry_max` 携带上限），
  与"分档阈值不放配置"是同一套纪律（见 `core/model.py` 的 `TIER_BOUNDS`）。
- **不含无头随机**：不做抽卡/暴击。数值养成的手感来自可预期的规划，
  随机奖励会让"她饿了"变成赌博，且无法写确定性测试门。
  v0.7.0 的"每日特惠"不违反这条：它是 `(日期, 角色卡名)` 的确定性哈希——
  当天人人可复算，测试能钉死，只是用户每天看到不同的货架（见 `core/shop.py`）。
- **账本按"日"聚合**（`ledger_date` + 当日收支），不存逐条流水：
  面板只需要"今天挣了多少、花了多少"，逐条流水是 store 膨胀源。
- **`Advisor` 是纯计算**：不读时钟、不看 store，输入是"她的消耗画像 + 背包快照"，
  所以能直接单测"三天没回来会饿几天"这类问题。

与饱食档位的耦合：`MEAL_THRESHOLD` 落在 `satiety` 的 satisfied 档内
（下界 40），所以"她饿了要吃饭"这件事在面板上读作"饱食掉到『吃饱』以下的档"，
用户看到的两处信号一致（详见 `core/model.py` 的 `SATIETY_TIERS`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any, Iterable, Mapping

__all__ = [
    "DEFAULT_MEAL_UNITS",
    "ITEMS",
    "ITEM_IDS",
    "ITEM_ORDER",
    "MEAL_RESTORE",
    "MEAL_THRESHOLD",
    "Advisor",
    "Inventory",
    "Item",
    "PurchasePlan",
    "advise",
    "bump_meal_day",
    "daily_income",
    "effects_for",
    "item",
    "item_ids",
    "meal_need_per_day",
    "meal_plan",
    "meal_restore",
    "normalize_inventory",
    "observed_meals_per_day",
    "recharge_plan",
    "stock_days",
    "stock_meals",
    "total_units",
]

# 一餐把饱食补到哪里（补到接近满，但不溢出）：`core/model.apply_meal` 用它。
# 默认值给"正餐"，另外按物品单独覆盖（点心补得少）——这张表是单一来源，
# `_ITEM_TABLE` 里每一项的 satiety 效果就来自它，避免"表里写 38、代码里写 30"这种漂移。
MEAL_RESTORE = 38.0
_MEAL_RESTORE_BY_ITEM: Mapping[str, float] = {
    "meat": 38.0,
    "fish": 38.0,
    "cake": 22.0,
    "pudding": 20.0,
    "royal": 45.0,
}
# 低于这个饱食值她就想吃饭（落在 satisfied 档内，见模块 docstring）
MEAL_THRESHOLD = 55.0
# 没有历史数据时，面板给出的兜底每日餐数
DEFAULT_MEAL_UNITS = 2.0


def meal_restore(item_id: str) -> float:
    """吃掉某样东西补多少饱食（未知物品退回默认正餐量）。"""
    return _MEAL_RESTORE_BY_ITEM.get(item_id, MEAL_RESTORE)


@dataclass(frozen=True, slots=True)
class Item:
    """一件可购买、可持有的东西。

    `effects` 是"用了它，哪几项数值加多少"（0..100 的绝对增量，由调用方夹取）。
    `food` 标记它能不能当饭吃掉——只有 food 物品会被自动进食消耗。
    `cost_sodas` 是**标价**（金币），`carry_max` 是携带上限（0 = 不限）。

    v0.7.0 商店深化新增（解锁判据与折扣价在 `core/shop.py`，那张表才是货架规则）：

    - `rarity`：`common` / `uncommon` / `rare` 三档纯标签。它**不改变任何数值语义**，
      只是面板徽章与折扣文案的呈现层——稀有度不是解锁判据，解锁只看 `shop.py` 的规则。
    - `keepsake`：收藏件。买断后**永久持有、不可被消耗**（`feed` 入口拒绝），
      持有期间每天产出 `daily`。`carry_max=1` 保证同一件只会拥有一份。
    - `daily`：持有型产出（`((stat, 每天量), ...)`）。键可以是 `coins`——它不是五轴
      数值，所以产出结算里金币走独立加法，轴数值才走 `apply_item` 的夹取路径。
    """

    id: str
    kind: str
    cost_sodas: int
    effects: tuple[tuple[str, float], ...]
    food: bool = False
    carry_max: int = 0
    order: int = 0
    rarity: str = "common"
    keepsake: bool = False
    daily: tuple[tuple[str, float], ...] = ()

    def effect(self, stat: str) -> float:
        for name, delta in self.effects:
            if name == stat:
                return delta
        return 0.0


# 物品表（单一来源）。id 是稳定 ASCII 键，面向用户的名称走 i18n `panel.item.<id>`。
_ITEM_TABLE: tuple[Item, ...] = (
    Item(
        id="meat",
        kind="food",
        cost_sodas=6,
        effects=(("satiety", MEAL_RESTORE), ("mood", 3.0)),
        food=True,
        order=10,
    ),
    Item(
        id="fish",
        kind="food",
        cost_sodas=10,
        effects=(("satiety", MEAL_RESTORE), ("mood", 6.0), ("health", 2.0)),
        food=True,
        order=20,
    ),
    Item(
        id="cake",
        kind="food",
        cost_sodas=9,
        effects=(("satiety", _MEAL_RESTORE_BY_ITEM["cake"]), ("mood", 10.0)),
        food=True,
        order=30,
    ),
    # v0.7.0 商店深化：四件新货。解锁线全部读**已有后端计数器**
    # （累计班次 / 最长连签 / 好感档 / 当日幸运签），不新造任何状态
    # （唯一例外是礼盒的"解锁不回退"，那是分片里的 `shop_unlocked` 锁存账，见 core/shop.py）。
    Item(
        id="pudding",
        kind="food",
        cost_sodas=15,
        effects=(("satiety", _MEAL_RESTORE_BY_ITEM["pudding"]), ("mood", 15.0)),
        food=True,
        order=40,
        rarity="uncommon",
    ),
    Item(
        id="royal",
        kind="food",
        cost_sodas=18,
        effects=(("satiety", _MEAL_RESTORE_BY_ITEM["royal"]), ("mood", 8.0), ("health", 5.0)),
        food=True,
        order=50,
        rarity="rare",
    ),
    Item(
        id="medicine",
        kind="care",
        cost_sodas=20,
        effects=(("health", 18.0), ("energy", 6.0)),
        carry_max=5,
        order=60,
        rarity="uncommon",
    ),
    Item(
        id="toy",
        kind="toy",
        cost_sodas=14,
        effects=(("mood", 12.0), ("energy", 8.0)),
        carry_max=3,
        order=70,
        rarity="uncommon",
    ),
    Item(
        id="gift",
        kind="gift",
        cost_sodas=30,
        effects=(("affection", 4.0), ("mood", 8.0)),
        carry_max=5,
        order=80,
        rarity="rare",
    ),
    # 收藏件：不可消耗（feed 入口以 `item_keepsake` 拒绝），持有期间每天产出。
    Item(
        id="giftbox",
        kind="gift",
        cost_sodas=60,
        effects=(),
        carry_max=1,
        order=90,
        rarity="rare",
        keepsake=True,
        daily=(("mood", 2.0),),
    ),
    Item(
        id="charm",
        kind="charm",
        cost_sodas=88,
        effects=(),
        carry_max=1,
        order=100,
        rarity="rare",
        keepsake=True,
        daily=(("coins", 3.0),),
    ),
)

ITEMS: Mapping[str, Item] = {entry.id: entry for entry in _ITEM_TABLE}
ITEM_IDS: tuple[str, ...] = tuple(entry.id for entry in _ITEM_TABLE)
# 面板与文案用的稳定顺序（按 order，不依赖 dict 顺序）
ITEM_ORDER: tuple[str, ...] = tuple(
    entry.id for entry in sorted(_ITEM_TABLE, key=lambda entry: (entry.order, entry.id))
)


def item(item_id: str) -> Item | None:
    return ITEMS.get(item_id) if isinstance(item_id, str) else None


def item_ids() -> tuple[str, ...]:
    return ITEM_ORDER


def effects_for(item_id: str) -> dict[str, float]:
    """物品的效果表（拷贝，调用方可以随便改）；未知物品返回空表。"""
    found = item(item_id)
    return {} if found is None else {name: delta for name, delta in found.effects}


# ---------------------------------------------------------------------------
# 背包
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Inventory:
    """背包：`{物品 id: 数量}`，只保留正数量且只保留已知物品。"""

    counts: tuple[tuple[str, int], ...] = ()

    def get(self, item_id: str) -> int:
        for name, count in self.counts:
            if name == item_id:
                return count
        return 0

    def as_dict(self) -> dict[str, int]:
        return {name: count for name, count in self.counts}

    @property
    def total(self) -> int:
        return sum(count for _name, count in self.counts)

    def with_added(self, item_id: str, amount: int) -> "Inventory":
        if amount == 0 or item(item_id) is None:
            return self
        counts = self.as_dict()
        counts[item_id] = max(0, counts.get(item_id, 0) + int(amount))
        return Inventory.from_mapping(counts)

    def with_consumed(self, item_id: str, amount: int) -> "Inventory":
        return self.with_added(item_id, -abs(int(amount)))

    def food_units(self) -> int:
        """能当饭吃的总份数（口粮顾问的分母之一）。"""
        return sum(count for name, count in self.counts if (found := item(name)) is not None and found.food)

    def in_stock(self, wanted: Iterable[str]) -> bool:
        return all(self.get(name) > 0 for name in wanted)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Inventory":
        """从持久化数据恢复：丢弃未知物品、非正数量与非法值。

        刻意**不保留**未知物品：物品表是单一来源，留着一个当前版本不认识的 id
        只会在面板上显示成空名字，且会随着物品表演进长期堆积。
        """
        if not isinstance(raw, Mapping):
            return cls()
        counts: dict[str, int] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or item(key) is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            amount = int(value)
            if amount > 0:
                counts[key] = amount
        return cls(tuple(sorted(counts.items())))


def normalize_inventory(raw: Mapping[str, Any] | None) -> Inventory:
    """`Inventory.from_mapping` 的别名（服务层与测试都读这个更直白）。"""
    return Inventory.from_mapping(raw)


def total_units(inventory: Inventory | Mapping[str, Any] | None) -> int:
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    return inv.total


def stock_meals(inventory: Inventory | Mapping[str, Any] | None) -> int:
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    return inv.food_units()


def stock_days(meals: int | float, *, per_day: float) -> float:
    """这些餐还能撑几天（`per_day <= 0` 时视为"不会饿"，返回无穷）。"""
    if per_day <= 0.0:
        return float("inf")
    return max(0.0, float(meals)) / float(per_day)


# ---------------------------------------------------------------------------
# 吃饭
# ---------------------------------------------------------------------------


def meal_plan(
    inventory: Inventory | Mapping[str, Any] | None,
    *,
    staple_item_id: str = "meat",
    satiety: float,
    threshold: float = MEAL_THRESHOLD,
) -> str | None:
    """返回这一餐该吃的物品 id；不该吃（还没饿 / 没粮）时返回 None。

    选择顺序：**指定口粮优先**（主人囤的就是它），没有就用背包里其它能当饭的东西，
    按物品表顺序（便宜的先吃）。
    """
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    if satiety >= threshold:
        return None
    preferred = item(staple_item_id)
    if preferred is not None and preferred.food and inv.get(preferred.id) > 0:
        return preferred.id
    for candidate_id in ITEM_ORDER:
        found = ITEMS[candidate_id]
        if found.food and inv.get(candidate_id) > 0:
            return candidate_id
    return None


# ---------------------------------------------------------------------------
# 购买
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PurchasePlan:
    """一次购买的可执行性判定（`ok=False` 时 `reason` 是稳定 ASCII 码）。"""

    ok: bool
    item_id: str
    quantity: int
    unit_cost: int
    total_cost: int
    reason: str = ""
    carry_left: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "item_id": self.item_id,
            "quantity": self.quantity,
            "unit_cost": self.unit_cost,
            "total_cost": self.total_cost,
            "reason": self.reason,
            "carry_left": self.carry_left,
        }


def recharge_plan(
    *,
    item_id: str,
    quantity: int,
    inventory: Inventory | Mapping[str, Any] | None,
    coins: int,
    daily_spent: int = 0,
    daily_limit: int = 0,
    unit_cost: int | None = None,
) -> PurchasePlan:
    """买 `quantity` 个 `item_id` 能不能成。

    `reason` 用稳定码，供面板直接翻译：
    `unknown_item` / `invalid_quantity` / `insufficient_sodas` / `over_daily_limit` / `carry_full`。

    `unit_cost` 是**调用方复核过的成交单价**（今日特惠由 `core/shop.py` 算好后传进来）。
    缺省用标价。这里刻意不做折扣判断——折扣真相只有一处来源，
    入口传错单价的锅在调用方，本函数仍然是纯算。
    """
    found = item(item_id)
    if found is None:
        return PurchasePlan(False, str(item_id), 0, 0, 0, reason="unknown_item")
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        return PurchasePlan(False, found.id, 0, found.cost_sodas, 0, reason="invalid_quantity")
    amount = int(quantity)
    if amount <= 0:
        return PurchasePlan(False, found.id, 0, found.cost_sodas, 0, reason="invalid_quantity")
    price = found.cost_sodas if unit_cost is None else max(1, int(unit_cost))
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    total = price * amount
    carry_left = 0
    if found.carry_max > 0:
        carry_left = max(0, found.carry_max - inv.get(found.id))
        if amount > carry_left:
            return PurchasePlan(
                False, found.id, amount, price, total, reason="carry_full", carry_left=carry_left
            )
    if int(coins) < total:
        return PurchasePlan(
            False, found.id, amount, price, total, reason="insufficient_sodas", carry_left=carry_left
        )
    if daily_limit > 0 and int(daily_spent) + total > int(daily_limit):
        return PurchasePlan(
            False, found.id, amount, price, total, reason="over_daily_limit", carry_left=carry_left
        )
    return PurchasePlan(True, found.id, amount, price, total, carry_left=carry_left)


# ---------------------------------------------------------------------------
# 日薪
# ---------------------------------------------------------------------------


def daily_income(
    *,
    turns: int,
    is_new_day: bool,
    streak_days: int,
    turn_reward: float = 0.6,
    new_day_bonus: float = 4.0,
    streak_reward: float = 0.7,
    streak_cap: float = 10.0,
    max_per_session: int = 20,
) -> int:
    """"今天挣了多少"——按互动结算的日薪。

    刻意**不给无限刷**：单次结算最多认 `max_per_session` 次发言，
    所以一天泡在对话里也不会刷爆经济（想多挣只能多天连续相处）。
    """
    counted = max(0, min(int(turns), int(max_per_session)))
    total = counted * max(0.0, turn_reward)
    if is_new_day:
        total += max(0.0, new_day_bonus)
    if is_new_day and streak_days > 0:
        total += min(max(0.0, streak_reward) * max(0, int(streak_days) - 1), max(0.0, streak_cap))
    return int(round(total))


# ---------------------------------------------------------------------------
# 口粮顾问
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Advisor:
    """"她每天吃多少、口粮还够几天、缺口有多大"。

    - `meal_need_per_day`：模型推算的每日需求（理论值，来自饱食衰减速率 + 作息）。
    - `observed_meals_per_day`：最近实际吃掉的餐/天（没数据时为 0，用理论值兜底）。
    - `meals_per_day`：上面两者取大（保守：宁可多囤）。
    - `stock_meals`：当前背包里的口粮份数。
    - `days_remaining`：按 `meals_per_day` 还能撑几天。
    - `shortfall_meals`：撑满 `horizon_days` 还缺几份。
    - `suggested_purchase`：补足 `horizon_days` 需要买几份指定口粮。
    - `urgent`：`days_remaining < warn_days`（面板据此标红）。
    """

    meal_need_per_day: float
    observed_meals_per_day: float
    meals_per_day: float
    stock_meals: int
    days_remaining: float
    horizon_days: int
    shortfall_meals: int
    suggested_purchase: int
    suggested_cost: int
    coins: int
    affordable: bool
    urgent: bool
    empty: bool
    staple_item_id: str
    theoretical_meals_per_day: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "meal_need_per_day": round(self.meal_need_per_day, 2),
            "observed_meals_per_day": round(self.observed_meals_per_day, 2),
            "meals_per_day": round(self.meals_per_day, 2),
            "stock_meals": self.stock_meals,
            "days_remaining": None if self.days_remaining == float("inf") else round(self.days_remaining, 2),
            "horizon_days": self.horizon_days,
            "shortfall_meals": self.shortfall_meals,
            "suggested_purchase": self.suggested_purchase,
            "suggested_cost": self.suggested_cost,
            "coins": self.coins,
            "affordable": self.affordable,
            "urgent": self.urgent,
            "empty": self.empty,
            "staple_item_id": self.staple_item_id,
            "theoretical_meals_per_day": round(self.theoretical_meals_per_day, 2),
        }


def meal_need_per_day(
    *,
    per_day_decay: float,
    restore_per_meal: float = MEAL_RESTORE,
    satiety: float = 100.0,
    threshold: float = MEAL_THRESHOLD,
) -> float:
    """理论每日餐数：一天从满掉到必须吃饭一次，需要消耗多少。

    公式：`(每日饱食消耗 + 一餐缓冲) / 一餐补充量`。
    其中"一餐缓冲"是 `100 - threshold`（她不会等到 0 才吃，掉到阈值就该吃），
    所以默认参数下就是 `(消耗 + 45) / 38` —— 消耗 60/天时约 2.8 餐/天。
    """
    if restore_per_meal <= 0.0:
        return 0.0
    consumed = max(0.0, float(per_day_decay))
    buffer = max(0.0, min(100.0, float(satiety)) - max(0.0, float(threshold)))
    if buffer <= 0.0:
        buffer = max(0.0, min(100.0, float(satiety)))
    return max(1.0, (consumed + buffer) / float(restore_per_meal))


def advise(
    *,
    inventory: Inventory | Mapping[str, Any] | None,
    coins: int,
    meal_need: float,
    observed_meals_per_day: float = 0.0,
    staple_item_id: str = "meat",
    horizon_days: int = 7,
    warn_days: float = 2.0,
) -> Advisor:
    """把"她的消耗画像 + 背包 + 金币"折成一份面板可直出的建议。"""
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    observed = max(0.0, float(observed_meals_per_day))
    theoretical = max(0.0, float(meal_need))
    per_day = max(theoretical, observed)
    meals = inv.food_units()
    remaining = stock_days(meals, per_day=per_day)
    needed = int(ceil(per_day * max(1, int(horizon_days))))
    shortfall = max(0, needed - meals)
    found = item(staple_item_id)
    unit_cost = found.cost_sodas if found is not None else 0
    suggested = shortfall
    cost = suggested * unit_cost
    coins_now = max(0, int(coins))
    # 紧急 = 撑不到 warn_days；**或者**口粮已经见底且补不起货（钱不够同样是死局）
    urgent = remaining < max(0.0, float(warn_days)) or (meals <= 0 and coins_now < cost)
    affordable = cost <= 0 or coins_now >= cost
    return Advisor(
        meal_need_per_day=theoretical,
        observed_meals_per_day=observed,
        meals_per_day=per_day,
        stock_meals=meals,
        days_remaining=remaining,
        horizon_days=max(1, int(horizon_days)),
        shortfall_meals=shortfall,
        suggested_purchase=suggested,
        suggested_cost=cost,
        coins=coins_now,
        affordable=affordable,
        urgent=urgent,
        empty=meals <= 0,
        staple_item_id=found.id if found is not None else "",
        theoretical_meals_per_day=theoretical,
    )


def observed_meals_per_day(meal_days: Iterable[tuple[str, int]], *, window_days: int = 7) -> float:
    """最近 `window_days` 天的实测餐数均值（没记录返回 0，交给调用方兜底）。

    分母用**有记录的天数**而不是整个窗口：她可能才刚开始过日子，
    用 7 去摊会把"一天吃 3 餐"稀释成 0.43，面板上就会给出荒谬的建议。
    """
    entries = [(str(day), int(count)) for day, count in meal_days if day and int(count) > 0]
    if not entries:
        return 0.0
    horizon = max(1, int(window_days))
    recent = sorted(entries, key=lambda pair: pair[0])[-horizon:]
    return sum(count for _day, count in recent) / float(len(recent))


def bump_meal_day(meal_days: Iterable[tuple[str, int]], day: str, *, keep: int = 14) -> tuple[tuple[str, int], ...]:
    """记一次吃饭到当日计数里，保持有界（面板只需要最近两周）。"""
    if not day:
        return tuple(sorted((str(d), int(c)) for d, c in meal_days if d))[-keep:]
    counts = {str(d): int(c) for d, c in meal_days if d}
    counts[day] = counts.get(day, 0) + 1
    return tuple(sorted(counts.items())[-keep:])
