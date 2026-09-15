"""商店层：解锁规则、货架可见性、每日特惠与收藏件产出——纯函数，零 SDK 依赖。

本模块回答三个问题（v0.7.0「商店深化」主线）：

1. **什么货会上架**（`visible_ids`）：老 6 件常驻；皇家肉干 / 布丁 / 限定礼盒
   达成解锁线后上架；幸运符只在**幸运签当日**出现、过日下架。
2. **今天卖什么价**（`daily_deals` / `unit_cost`）：`(日期, 角色卡名)` 的确定性哈希
   从**当前可见货架**里挑 1~2 件 -30%。面板显示的价只是预告，
   入口成交前必须回到这里复算（威胁模型与小游戏同一层：`api.call` 参数可伪造）。
3. **收藏件每天产什么**（`keepsake_daily` / `keepsake_yield_days`）：买断后按日界线
   结算发放，跨多天未上线按缺席天数补发（封顶 `YIELD_BACKFILL_CAP` 天）。

设计取舍（都写进 docstring，避免后人当成疏漏）：

- **解锁判据全部读已有后端计数器**（累计班次 / 最长连签 / 好感档 / 当日幸运签），
  不新造业务状态。唯一的例外是「解锁不回退」这条契约：好感档是会掉的，
  而限定礼盒一旦被她见识过就不能再收回，所以有一本 `shop_unlocked` 锁存账
  （分片字段，读写在 `services/state.py`）。皇家肉干 / 布丁的判据本身单调
  （累计值只涨不跌），进锁存账只是为了货架读数**只增不减**这一条体验承诺。
- **幸运符刻意不进锁存账**：它的卖点就是"错过今天等下次"。锁存一次就永久
  在售，随机货架位变成常驻商品，稀缺感归零。
- **折扣基数是标价，不是"上一轮折扣价"**：特惠逐日独立抽签，没有复利折扣。
- **稀有度不是解锁判据**：`rarity` 只是面板徽章的呈现层（单一来源在
  `core/economy.py` 的物品表）。本模块不读它做任何判定——
  否则"把某件改成 common"会静默改变折扣池语义。
- **可见性判定是纯函数**：输入是"事实四元组 + 锁存账"，所以"她跑满 10 个班
  那刻货架多出一张卡"这种时刻线可以直接单测，不需要模拟运行时。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Iterable, Mapping, Sequence

from .economy import ITEM_ORDER, Inventory, item

__all__ = [
    "DISCOUNT_PCT",
    "GATED_DAILY",
    "GATED_PERMANENT",
    "PUDDING_MIN_STREAK",
    "RARITIES",
    "ROYAL_MIN_SHIFTS",
    "UNLOCKED_GIFTBOX_TIER",
    "YIELD_BACKFILL_CAP",
    "UnlockFacts",
    "condition_met",
    "daily_deals",
    "discounted_cost",
    "fresh_unlocks",
    "keepsake_daily",
    "keepsake_yield_days",
    "normalize_unlocks",
    "unit_cost",
    "visible_ids",
]

# 稀有度词表（呈现层单一来源；面板徽章按 `panel.rarity.<值>` 拼键翻译）。
RARITIES = ("common", "uncommon", "rare")

# 解锁线。皇家肉干：累计班次；布丁：历史最长连签；限定礼盒：好感达到
# `UNLOCKED_GIFTBOX_TIER` 档（含）以上即算"见识过亲密关系"。
ROYAL_MIN_SHIFTS = 10
PUDDING_MIN_STREAK = 7
UNLOCKED_GIFTBOX_TIER = "close"

# 永久解锁线（进锁存账，达成后永不回退）与"当日限定"（刻意不锁存）。
GATED_PERMANENT = ("royal", "pudding", "giftbox")
GATED_DAILY = ("charm",)

# 每日特惠：抽 1~2 件，折扣百分比固定（不是区间随机——"标价 ×0.7" 心算可验，
# 面板划线价才有公信力）。
DISCOUNT_PCT = 30
_DEALS_SEED_PREFIX = "our_life.deal.v1"

# 收藏件产出的跨天补发封顶（天）。零花钱的惯例是"跨天只发当天一份"，
# 收藏件刻意不同（"不上线也在攒"是它的卖点），但封顶防止一次长假
# 回来凭空多出一大笔——7 天以上的缺席按 7 天算。
YIELD_BACKFILL_CAP = 7


class UnlockFacts:
    """解锁判据的"事实四元组"。

    刻意用普通类而不是 dataclass frozen slots：字段就这四个，构造点在读侧
    （入口 / tick / dashboard），没有做相等性比较的需求——保持零依赖直觉。
    """

    __slots__ = ("job_shifts_total", "checkin_best", "affection_close", "lucky_today")

    def __init__(self, *, job_shifts_total: int, checkin_best: int, affection_close: bool, lucky_today: bool) -> None:
        self.job_shifts_total = max(0, int(job_shifts_total))
        self.checkin_best = max(0, int(checkin_best))
        self.affection_close = bool(affection_close)
        self.lucky_today = bool(lucky_today)

    def as_dict(self) -> dict[str, object]:
        return {
            "job_shifts_total": self.job_shifts_total,
            "checkin_best": self.checkin_best,
            "affection_close": self.affection_close,
            "lucky_today": self.lucky_today,
        }


def condition_met(item_id: str, facts: UnlockFacts) -> bool:
    """此刻（不含锁存账）她的读数是否满足 `item_id` 的解锁线。常驻货恒为 True。"""
    if item_id == "royal":
        return facts.job_shifts_total >= ROYAL_MIN_SHIFTS
    if item_id == "pudding":
        return facts.checkin_best >= PUDDING_MIN_STREAK
    if item_id == "giftbox":
        return facts.affection_close
    if item_id == "charm":
        return facts.lucky_today
    return item(item_id) is not None


def visible_ids(facts: UnlockFacts, latched: Iterable[str]) -> tuple[str, ...]:
    """当前货架上的商品 id（顺序 = 物品表顺序）。

    锁存账只对永久件生效——幸运符即使昨天出现在 `latched` 里（脏数据）也
    不会因它上架：判据只认 `facts.lucky_today`。
    """
    latched_set = {str(name) for name in latched}
    return tuple(
        item_id
        for item_id in ITEM_ORDER
        if item(item_id) is not None and _visible_one(item_id, facts, latched_set)
    )


def _visible_one(item_id: str, facts: UnlockFacts, latched: set[str]) -> bool:
    if item_id in GATED_DAILY:
        return condition_met(item_id, facts)
    if item_id in GATED_PERMANENT:
        return item_id in latched or condition_met(item_id, facts)
    return True


def fresh_unlocks(facts: UnlockFacts, latched: Iterable[str]) -> tuple[str, ...]:
    """这次读数新满足、还没进锁存账的永久件（调用方负责落盘）。"""
    latched_set = {str(name) for name in latched}
    return tuple(
        item_id
        for item_id in GATED_PERMANENT
        if item_id not in latched_set and condition_met(item_id, facts)
    )


def normalize_unlocks(raw: object) -> tuple[str, ...]:
    """锁存账读侧消毒：只认永久件 id、去重、升序、丢弃一切非法条目。

    幸运符**不允许**出现在锁存账里（即便旧数据误写，读回来也会被丢掉）——
    这是"当日限定"的行为保证，不是清理强迫症。
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    cleaned = {entry for entry in (str(x) for x in raw) if entry in GATED_PERMANENT}
    return tuple(sorted(cleaned))


# ---------------------------------------------------------------------------
# 每日特惠
# ---------------------------------------------------------------------------


def _digest(day: str, lanlan: str) -> bytes:
    """`SHA-256("前缀|日期|角色卡名")`。日期由调用方保证是 `YYYY-MM-DD`。"""
    seed = f"{_DEALS_SEED_PREFIX}|{day}|{lanlan}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def daily_deals(day: str, lanlan: str, pool: Sequence[str]) -> dict[str, int]:
    """今天打折的商品：`{item_id: 折扣百分比}`（对不在池里的 id 查询返回空表）。

    确定性：同一 (日期, 卡名, 池) 永远同一结果——面板每 10s 轮询、入口复算、
    测试重放三方必须看到同一份"今日价"。池为空或池只有鬼 id 时返回空表。
    """
    entries = [str(name) for name in pool if item(str(name)) is not None]
    if not entries:
        return {}
    digest = _digest(day, str(lanlan))
    # 件数：约 1/3 概率 1 件，其余 2 件（件数本身也是哈希的函数，不是第二次掷骰）。
    count = 1 if digest[0] % 3 == 0 else 2
    count = min(count, len(entries))
    picked: list[str] = []
    # 从摘要里连续取字节作"无放回抽签"：每轮重抽撞重复时顺延下一槽位。
    for index in range(count):
        slot = digest[1 + index] % len(entries)
        for offset in range(len(entries)):
            candidate = entries[(slot + offset) % len(entries)]
            if candidate not in picked:
                picked.append(candidate)
                break
    return {name: DISCOUNT_PCT for name in picked}


def discounted_cost(item_id: str, pct: int) -> int:
    """按折扣百分比打折（`pct=30` → 七折）。下限 1 金：白送会击穿日消费限额的意义。"""
    found = item(item_id)
    if found is None:
        return 0
    percent = max(0, min(100, int(pct)))
    return max(1, int(round(found.cost_sodas * (100 - percent) / 100.0)))


def unit_cost(day: str, lanlan: str, pool: Sequence[str], item_id: str) -> int:
    """成交单价的唯一复算入口：入口、面板目录、测试都走它（谁都不许自己算）。"""
    found = item(item_id)
    if found is None:
        return 0
    deals = daily_deals(day, lanlan, pool)
    if item_id in deals:
        return discounted_cost(item_id, deals[item_id])
    return found.cost_sodas


# ---------------------------------------------------------------------------
# 收藏件产出
# ---------------------------------------------------------------------------


def keepsake_daily(inventory: Inventory | Mapping[str, int]) -> dict[str, float]:
    """背包里所有收藏件的**每日总产出**：`{键: 量}`（键含 `coins`，它不是五轴）。

    `carry_max=1` 保证同件最多一份；这里仍按数量乘算而不是假设 1，
    这样未来放开"多份多产出"时不需要动这条函数。
    """
    inv = inventory if isinstance(inventory, Inventory) else Inventory.from_mapping(inventory)
    totals: dict[str, float] = {}
    for name, count in inv.counts:
        found = item(name)
        if found is None or not found.keepsake or count <= 0:
            continue
        for key, amount in found.daily:
            totals[key] = totals.get(key, 0.0) + float(amount) * count
    return totals


def keepsake_yield_days(prev_day: str, today: str, *, cap: int = YIELD_BACKFILL_CAP) -> int:
    """日界线从 `prev_day` 滚到 `today` 应发放几份产出。

    - 同一天 / 非法 / 空 `prev_day` → 0（**买入当天不产**，从次日日界线起算）。
    - 跨 N 天 → N 份，封顶 `cap`（用户拍板"按缺席天数补发"，与零花钱的
      "跨天只发当天一份"刻意不同，理由见模块 docstring）。
    """
    limit = max(1, int(cap))
    try:
        earlier = date.fromisoformat(str(prev_day))
        later = date.fromisoformat(str(today))
    except (TypeError, ValueError):
        return 0
    days = (later - earlier).days
    if days <= 0:
        return 0
    return min(days, limit)
