"""作息节律：时段判定、睡眠窗、纪念日——纯函数，零 SDK 依赖。

这一层存在的理由（v0.2.0「过日子」主线）：数值不再只由"你说了几句话"驱动，
而是由**她的一天怎么过**驱动。她的精力在睡眠窗里回复、在清醒时段里消耗；
她每天要吃饭；相处到某些天数是纪念日。这些都需要"现在是她一天的哪个时段"。

设计取舍：

1. **不猜作息**：首版不根据总线记录推断她几点睡，直接用配置的睡眠窗
   （`[our_life.rhythm].sleep_start_hour` / `sleep_end_hour`，支持跨零点）。
   理由：推断需要长期统计、错了会让"她半夜不困"这种明显错误暴露给用户；
   配置直白、面板可解释、纯函数可单测。
2. **时段是展示与文案用的概念**，数值计算只关心"睡没睡"。
   `phase` 只用于面板与注入文案（`core/injection.py` 的"现在几点"提示）。
3. **跨零点**：`sleep_start_hour > sleep_end_hour` 表示夜里跨零点（默认 24 → 8）。
   `24` 是合法的"当天结束"锚点（代表 00:00），见 `_normalize_hour`。
4. **纪念日按"相处天数"算**，不按日历日期：相处第 3 / 7 / 14 / 30 / 100 天与每个整年。
   "相处天数"用 `core.model.day_number_of`（基于 `first_day`）算，不依赖 streak，
   这样断档之后纪念日不会错位。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil

__all__ = [
    "ANNIVERSARY_DAYS",
    "DAY_SECONDS",
    "Anniversary",
    "DailyRhythm",
    "anniversary_of",
    "anniversary_of_day_number",
    "day_number_of",
    "days_until_next_anniversary",
    "is_anniversary",
    "is_sleep_hour",
    "minutes_until_next_boundary",
    "normalize_hour",
    "overlap_hours",
    "phase_of",
    "resolve_rhythm",
]

DAY_SECONDS = 86400.0

# 纪念日锚点：相处第 N 天。1 = 相遇当天（不算"纪念日事件"，由首次问候负责）。
ANNIVERSARY_DAYS: tuple[int, ...] = (3, 7, 14, 30, 60, 100, 180, 365)


def normalize_hour(hour: int) -> int:
    """把小时规整到 0..23。

    特判：`24` 代表"当天结束"（等价于次日的 00:00），这是配置里最自然的写法
    （睡觉时间 24 点、起床 8 点），所以先映射成 0 而不是抛错。
    """
    value = int(hour) % 24
    return value if value >= 0 else value + 24


def phase_of(hour: int) -> str:
    """时段名（稳定 ASCII 键，面向用户的文案走 i18n `panel.phase.<键>`）。

    late_night 与 night 分开：前者是"该睡了还没睡"，文案与数值影响都不同
    （`core/model.py` 里 late_night 的精力消耗更高）。
    """
    normalized = normalize_hour(hour)
    if 5 <= normalized < 9:
        return "morning"
    if 9 <= normalized < 12:
        return "forenoon"
    if 12 <= normalized < 14:
        return "noon"
    if 14 <= normalized < 18:
        return "afternoon"
    if 18 <= normalized < 23:
        return "evening"
    return "late_night" if normalized >= 23 else "night"


def _minutes_of_hour(hour: int) -> int:
    return normalize_hour(hour) * 60


def _within(hour: int, start_hour: int, end_hour: int) -> bool:
    """`hour` 是否落在 `[start_hour, end_hour)` 内（支持跨零点）。"""
    start = normalize_hour(start_hour)
    end = normalize_hour(end_hour)
    value = normalize_hour(hour)
    if start == end:
        # 退化配置（起止相同）：按"整天都不睡"处理，避免把一天全算成睡眠。
        return False
    if start < end:
        return start <= value < end
    return value >= start or value < end


def is_sleep_hour(hour: int, *, sleep_start_hour: int, sleep_end_hour: int) -> bool:
    """某个整点是否落在睡眠窗内（睡眠窗用 [start, end) 的半开区间）。"""
    return _within(hour, sleep_start_hour, sleep_end_hour)


def overlap_hours(
    start: datetime,
    end: datetime,
    *,
    sleep_start_hour: int,
    sleep_end_hour: int,
) -> float:
    """整点粒度上统计 `[start, end)` 里的睡眠小时数。

    实现：从 `start` 的整点起逐时推进到 `end` 之前，统计每个整点落在睡眠窗内的个数。
    粒度是**整点**而不是分钟——衰减本来就是一小时量级的过程，整点足够，
    且不必处理分钟级边界。`end <= start` 时返回 0。
    """
    if end <= start:
        return 0.0
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(hours=1)
    hours = 0.0
    while cursor < end:
        if is_sleep_hour(cursor.hour, sleep_start_hour=sleep_start_hour, sleep_end_hour=sleep_end_hour):
            hours += 1.0
        cursor += timedelta(hours=1)
    return hours


def resolve_rhythm(now: float, *, sleep_start_hour: int, sleep_end_hour: int) -> "DailyRhythm":
    """把时间戳解成一份"她的此刻"快照。"""
    moment = datetime.fromtimestamp(float(now))
    hour = moment.hour
    sleeping = is_sleep_hour(hour, sleep_start_hour=sleep_start_hour, sleep_end_hour=sleep_end_hour)
    # 睡眠窗 = [sleep_start, sleep_end) 的环绕长度；清醒窗 = 一天减去它
    sleep_window = _window_hours(start_hour=sleep_start_hour, end_hour=sleep_end_hour)
    sleep_span = min(24.0, sleep_window)
    wake_span = 24.0 - sleep_span
    return DailyRhythm(
        now=float(now),
        hour=hour,
        minute=moment.minute,
        date_iso=moment.date().isoformat(),
        phase=phase_of(hour),
        sleeping=sleeping,
        sleep_hours=sleep_span,
        wake_hours=wake_span,
        awake_ratio=(wake_span / 24.0) if sleep_span < 24.0 else 1.0,
        hours_to_sleep=_hours_until(moment, sleep_start_hour),
        hours_to_wake=_hours_until(moment, sleep_end_hour),
    )


def _window_hours(*, start_hour: int, end_hour: int) -> float:
    """`[start, end)` 在一天里的环绕长度（跨零点照算）。

    两端规整到 0..23 后取正向环绕差；起止相同视为"这个窗不存在"（返回 0），
    由调用方决定它意味着"整天不睡"。
    """
    start = normalize_hour(start_hour)
    end = normalize_hour(end_hour)
    if start == end:
        return 0.0
    return float((end - start) % 24)


def _hours_until(moment: datetime, hour: int) -> float:
    """距下一个指定整点还有多少小时（0..24）。"""
    target = moment.replace(hour=normalize_hour(hour), minute=0, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return round((target - moment).total_seconds() / 3600.0, 4)


def minutes_until_next_boundary(now: float, rhythm: "DailyRhythm") -> int:
    """距下一个作息边界（睡下 / 醒来）还有多少分钟——面板的"她还有多久睡/醒"。"""
    hours = rhythm.hours_to_sleep if not rhythm.sleeping else rhythm.hours_to_wake
    return int(ceil(max(0.0, hours) * 60.0))


@dataclass(frozen=True, slots=True)
class DailyRhythm:
    """"她此刻的一天"：时段、睡眠状态、窗口时长、距边界的距离。"""

    now: float
    hour: int
    minute: int
    date_iso: str
    phase: str
    sleeping: bool
    sleep_hours: float
    wake_hours: float
    awake_ratio: float
    hours_to_sleep: float
    hours_to_wake: float

    def as_dict(self) -> dict[str, object]:
        return {
            "hour": self.hour,
            "minute": self.minute,
            "date": self.date_iso,
            "phase": self.phase,
            "sleeping": self.sleeping,
            "sleep_hours": self.sleep_hours,
            "wake_hours": self.wake_hours,
            "hours_to_sleep": self.hours_to_sleep,
            "hours_to_wake": self.hours_to_wake,
        }


# ---------------------------------------------------------------------------
# 纪念日
# ---------------------------------------------------------------------------


def day_number_of(first_day: str | None, today: str) -> int:
    """相处天数（相遇当天 = 1）。`first_day` 缺失/非法时返回 0（表示还不知道）。"""
    if not first_day or not today:
        return 0
    try:
        start = date.fromisoformat(first_day)
        current = date.fromisoformat(today)
    except (TypeError, ValueError):
        return 0
    delta = (current - start).days
    return delta + 1 if delta >= 0 else 0


def anniversary_of_day_number(day_number: int) -> "Anniversary | None":
    """相处天数是否命中纪念日锚点（含"每个整年"的重复纪念）。

    一年口径是**相处满 365 天**（`day_number % 365 == 0`，一年一遇、不重复触发）：
    锚点表里已经有 365，所以"第 1 年"由锚点负责，这里只管 730 / 1095 …
    """
    if day_number < 1:
        return None
    if day_number in ANNIVERSARY_DAYS:
        return Anniversary(kind="milestone", day_number=day_number, repeats_annually=False, years=0)
    if day_number > ANNIVERSARY_DAYS[-1] and day_number % 365 == 0:
        years = day_number // 365
        return Anniversary(kind="yearly", day_number=day_number, repeats_annually=True, years=years)
    return None


def anniversary_of(first_day: str | None, today: str) -> "Anniversary | None":
    return anniversary_of_day_number(day_number_of(first_day, today))


def is_anniversary(first_day: str | None, today: str) -> bool:
    return anniversary_of(first_day, today) is not None


def days_until_next_anniversary(day_number: int) -> int | None:
    """距下一个纪念日还有几天（相处天数口径）。没有下一个时返回 None。

    与 `anniversary_of_day_number` 共用同一套锚点，所以面板显示"还有 N 天"
    与届时真的触发事件是同一个判据，不会出现"面板说还有 0 天但没触发"。
    """
    if day_number < 0:
        return None
    if day_number == 0:
        return ANNIVERSARY_DAYS[0]
    for milestone in ANNIVERSARY_DAYS:
        if milestone > day_number:
            return milestone - day_number
    # 越过锚点表（365 天）之后只剩"每个整年"，一年一遇且不重复触发
    return 365 - (day_number % 365) if day_number % 365 else 365


@dataclass(frozen=True, slots=True)
class Anniversary:
    """一次纪念日事件。`kind` 是稳定 ASCII 键（i18n 用 `panel.anniversary.<kind>`）。"""

    kind: str
    day_number: int
    repeats_annually: bool
    years: int

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "day_number": self.day_number,
            "repeats_annually": self.repeats_annually,
            "years": self.years,
        }
