"""每日签到与日历（v0.7.0）：纯函数层，零 SDK 依赖。

她的一天从"打个卡"开始：翻开日历、点一下、领今天的金币。连续签到越久领得越多
（有封顶，防长尾通胀），偶尔"运气好"会多给一些（服务端随机，客户端只能展示）。
漏掉的日子可以花金币**补签**——补签不给钱，只把断掉的连续记录续上。

三条设计纪律（与 `core/economy.py` / `core/rhythm.py` 同源的做法）：

1. **连续天数从日历集合现算，不存增量计数器**。
   `current_streak(checked_days, today)` 是纯函数：签到日志 + 补签日子合成一个集合，
   连续记录是集合的读数。这样补签天然接链——昨天漏了、今天补上，连续天数自动变大，
   不需要任何"补签后要不要重算计数器"的特判（增量计数器迟早会和集合漂移，那是 bug 的
   温床）。存量的 `checkin_streak` 只是缓存显示值，每次签到/补签后都从集合重刷。
2. **随机是注入的**。`roll_luck(rng, ...)` 接受 `random.Random`，单测钉种子即可复现；
   生产入口用模块级 `_RNG`。掷骰子发生在**后端**：客户端只收到结果，没有作弊面。
3. **奖励是整数金币**。金币在本插件里全程是 `int`（与 `sodas` 一致），
   幸运加成 `round` 一次取整，不留小数尾巴。

补签的周计数用 ISO 周键（`2026-W37`）：跨周自动归零不需要常驻任务，
读的时候比较"存的那一周是不是本周"即可（与 `account_date` 的日账同一手法）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

__all__ = [
    "CHECKIN_LOG_KEEP",
    "CheckinOutcome",
    "apply_luck",
    "checked_days",
    "current_streak",
    "is_valid_day",
    "makeup_reason",
    "next_checkin_streak",
    "normalize_checkin_log",
    "normalize_makeups",
    "previous_day",
    "reward_coins",
    "roll_luck",
    "week_key",
]

# 签到日志保留天数：日历要能翻回上个月，120 天 ≈ 四个月，够看走势又不至于让分片膨胀。
CHECKIN_LOG_KEEP = 120


# ---------------------------------------------------------------------------
# 日期工具（ISO 字符串日；非法一律回退，不抛）
# ---------------------------------------------------------------------------


def is_valid_day(day: Any) -> bool:
    """是否为合法的 `YYYY-MM-DD` 日期串（面板/入口传来的 day 参数都先过这关）。"""
    if not isinstance(day, str) or len(day) != 10:
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def previous_day(day: str) -> str:
    """前一天（ISO 串）。非法输入返回空串（调用方按"没有前一天"处理）。"""
    parsed = _parse(day)
    if parsed is None:
        return ""
    return (parsed - timedelta(days=1)).isoformat()


def days_between(earlier: str, later: str) -> int | None:
    """`later - earlier` 的天数；任一非法返回 None。可以为负。"""
    left, right = _parse(earlier), _parse(later)
    if left is None or right is None:
        return None
    return (right - left).days


def week_key(day: str) -> str:
    """ISO 周键（`2026-W37`）。非法日期返回空串。"""
    parsed = _parse(day)
    if parsed is None:
        return ""
    iso = parsed.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------------------
# 台账归一化（store 里的脏数据不许炸读路径）
# ---------------------------------------------------------------------------


def normalize_checkin_log(raw: Any) -> tuple[tuple[str, int, bool], ...]:
    """签到日志：`[[day, coins, lucky], ...]` → 升序去重、逐条校验、限长。

    与 `_as_meal_days` 同一纪律：只接受认得的形状，认不出的条目直接丢弃，
    坏一条不连坐整本账。`lucky` 兼容 0/1 整数（store 里 bool 与 int 都可能遇到）。
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    merged: dict[str, tuple[int, bool]] = {}
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        day = entry[0] if is_valid_day(entry[0]) else None
        if day is None:
            continue
        coins = _coins_or_zero(entry[1] if len(entry) > 1 else 0)
        lucky = bool(entry[2]) if len(entry) > 2 else False
        merged[str(day)] = (max(0, coins), lucky)
    return tuple((day, coins, lucky) for day, (coins, lucky) in sorted(merged.items())[-CHECKIN_LOG_KEEP:])


def _coins_or_zero(value: Any) -> int:
    """金币读数容错：数字、数字字符串都收，其余归零（与 `core/coerce.as_int` 同一口径）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def normalize_makeups(raw: Any) -> tuple[str, ...]:
    """补签过的日子：合法 ISO 串、去重、升序、限长。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    days = {item for item in raw if is_valid_day(item)}
    return tuple(sorted(days))[-CHECKIN_LOG_KEEP:]


def checked_days(log: Iterable[tuple[str, Any]], makeups: Iterable[str]) -> frozenset[str]:
    """日历上"打过勾"的日子全集 = 正常签到 ∪ 补签。连续记录就是它的一段后缀。"""
    out: set[str] = set()
    for entry in log:
        day = entry[0] if isinstance(entry, (list, tuple)) and entry else None
        if is_valid_day(day):
            out.add(str(day))
    for day in makeups:
        if is_valid_day(day):
            out.add(str(day))
    return frozenset(out)


# ---------------------------------------------------------------------------
# 连续记录
# ---------------------------------------------------------------------------


def current_streak(checked: frozenset[str], today: str) -> int:
    """截至今天（或"今天还没签时"截至昨天）的连续签到天数。

    从集合后缀倒着数，不读任何计数器缓存——这是纪律 1 的实现点。
    """
    cursor = today
    if cursor not in checked:
        cursor = previous_day(cursor)
        if not cursor or cursor not in checked:
            return 0
    streak = 0
    while cursor and cursor in checked:
        streak += 1
        cursor = previous_day(cursor)
    return streak


def next_checkin_streak(checked: frozenset[str], today: str) -> int:
    """**如果今天签到**，连续天数会是多少（奖励按这个值结算）。

    昨天在集合里 → 昨天为后缀的连续长度 + 1；否则链条断在今天，就是 1。
    """
    yesterday = previous_day(today)
    if yesterday and yesterday in checked:
        return current_streak(checked, yesterday) + 1
    return 1


# ---------------------------------------------------------------------------
# 奖励
# ---------------------------------------------------------------------------


def reward_coins(
    *,
    streak: int,
    base_coins: int,
    streak_bonus_per_day: int,
    streak_cap_days: int,
) -> int:
    """基础 + 连续加成（加成天数封顶）。

    `streak` 是**含今天**的连续天数（`next_checkin_streak` 的输出），所以第 1 天
    拿基础值：加成按 `streak - 1` 计，最多 `streak_cap_days` 天——封顶是纪律，
    否则一年后每天 300+ 金币，经济就再也回不来了。
    """
    bonus_days = max(0, int(streak) - 1)
    bonus_days = min(bonus_days, max(0, int(streak_cap_days)))
    return max(0, int(base_coins)) + max(0, int(streak_bonus_per_day)) * bonus_days


def roll_luck(rng: random.Random, *, luck_chance: float, luck_min_bonus: float, luck_max_bonus: float) -> float:
    """掷一次"运气"：返回额外加成比例（0.0 = 没中）。

    命中概率夹在 [0, 1]；区间反写（max < min）时退化为固定 min——配置被手改坏
    最多让运气变差，不该让 tick/入口炸。
    """
    chance = max(0.0, min(1.0, float(luck_chance)))
    low = max(0.0, float(luck_min_bonus))
    high = max(low, float(luck_max_bonus))
    if rng.random() >= chance:
        return 0.0
    return rng.uniform(low, high)


def apply_luck(coins: int, extra_factor: float) -> tuple[int, bool]:
    """把加成比例落到整数金币上。返回 `(最终金币, 是否幸运)`。"""
    if extra_factor <= 0.0:
        return max(0, int(coins)), False
    return max(0, int(round(int(coins) * (1.0 + extra_factor)))), True


# ---------------------------------------------------------------------------
# 补签判定
# ---------------------------------------------------------------------------


def makeup_reason(
    *,
    day: str,
    today: str,
    checked: frozenset[str],
    used_this_week: int,
    week_limit: int,
    window_days: int,
) -> str:
    """能否补签这一天：返回 `"ok"` 或稳定错误码（进 codes.py 契约）。

    规则按顺序，第一条不过就返回它的码：
    - 非法日期 / 今天或未来（今天该走正常签到，未来还没发生）→ `makeup_invalid_day`
    - 已签到（含补签过）→ `already_checked_in`
    - 超出补签窗（默认 7 天）→ `makeup_expired`
    - 本周额度用完 → `makeup_exhausted`
    """
    if not is_valid_day(day) or not is_valid_day(today):
        return "makeup_invalid_day"
    gap = days_between(day, today)
    if gap is None or gap <= 0:
        return "makeup_invalid_day"
    if day in checked:
        return "already_checked_in"
    if gap > max(0, int(window_days)):
        return "makeup_expired"
    if used_this_week >= max(0, int(week_limit)):
        return "makeup_exhausted"
    return "ok"


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckinOutcome:
    """一次签到的结果（入口返回值与面板快照的单一形状来源）。"""

    day: str
    coins: int
    lucky: bool
    streak: int
    best: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "coins": self.coins,
            "lucky": self.lucky,
            "streak": self.streak,
            "best": self.best,
        }
