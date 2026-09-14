"""数值模型：纯函数、零 SDK 依赖。

**五根轴**（均 0..100）：

| 轴 | 周期 | 行为 |
|---|---|---|
| `satiety` 饱食 | 小时级 | 清醒时段掉得快、睡眠时段掉得慢；一餐补一大截（见 `apply_meal`） |
| `energy` 精力 | 小时级 | 清醒消耗、**睡眠窗内回复**；深夜熬夜额外多耗 |
| `mood` 心情 | 小时级 | 向静息基线回落（homeostasis）；饿着时掉得更快 |
| `health` 健康 | 天级 | 同样向静息基线回落；累着时恢复变慢；长期冷落额外扣 |
| `affection` 好感 | 周级 | **只缓慢回落、不自动回升**——它只能靠互动长 |

衰减是「读数时惰性折算」：`exp(-Δt/τ)` 把存储值折算到当前时刻，只在写入时落盘。
这样既不依赖常驻循环，也不怕进程重启，更避开了 timer 每拍新建 event loop 的陷阱。

**节律感知**：`apply_decay` 接受一份 `core.rhythm.DailyRhythm`。有它时，
饱食按"清醒/睡眠"分段积分、精力按"睡眠回复 / 清醒消耗"分段折算、
夜里熬到 23 点后额外多耗；没有它（纯测试或旧调用）时退化成"整天都清醒"，
公式与 v0.1.0 完全一致——**这是刻意保留的降级路径**，让模型层不必依赖时钟。

**跨轴耦合**（v0.2.0 新增，`apply_coupling`）：低饱食让心情掉得更快、健康恢复变慢；
低精力让健康恢复变慢。它不是"额外扣分"，而是**改变时间常数**——
所以饿一天的效果是"心情一路往下掉"，而不是"一次性 −5"，用户能看出因果。

分档阈值固定在 `TIER_BOUNDS`（单一来源，不进配置），档名是稳定 ASCII 键：
面向用户的档名走 i18n（`panel.tier.<stat>.<tier>`），面向模型的档名走 `core/injection.py`。

**两套分档读法，别混用**：

- `tier_of` / `tier_transitions`：硬比较（`value >= lower`），面板与"数值到底算哪一档"
  的忠实读法，必须原样反映每一个数值。
- `crosses_tier_boundary` / `eventful_tier_transitions`：**带迟滞**（`TIER_CROSSING_MARGIN`），
  只给**注入判定**用。分界线上会自然出现亚分噪声（默认好评 20.0 正好压着
  stranger/acquainted 的线，第一拍衰减就把它翻过去），硬比较会把这种噪声也当成"跨档事件"
  而白送一次强注入——详见 `TIER_CROSSING_MARGIN` 的病因记录。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import exp
from typing import Any, Iterable, Mapping

from .configuration import DecaySettings, GrowthSettings, InjectSettings, NeglectSettings
from .rhythm import DailyRhythm

__all__ = [
    "AFFECTION_TIERS",
    "COUPLING_THRESHOLDS",
    "ENERGY_TIERS",
    "HEALTH_TIERS",
    "MAX_VALUE",
    "MIN_VALUE",
    "MOOD_TIERS",
    "SATIETY_PER_HOUR_AWAKE",
    "SATIETY_PER_HOUR_SLEEP",
    "SATIETY_TIERS",
    "STAT_NAMES",
    "TIER_BOUNDS",
    "TIER_CROSSING_MARGIN",
    "CouplingSignal",
    "Stats",
    "advance_streak",
    "axis_details",
    "apply_anniversary",
    "apply_coupling",
    "apply_day_greet",
    "apply_decay",
    "apply_item",
    "apply_judgment",
    "apply_meal",
    "apply_neglect",
    "apply_streak_bonus",
    "apply_turn_gain",
    "clamp_value",
    "coupling_signal",
    "crisis_axes",
    "crosses_tier_boundary",
    "eventful_tier_transitions",
    "is_crisis",
    "neglect_entitlement_days",
    "satiety_per_day",
    "streak_milestone_bonus",
    "tier_index_of",
    "tier_of",
    "tier_transitions",
]

MIN_VALUE = 0.0
MAX_VALUE = 100.0

# 分档下界（升序）：0-19 / 20-39 / 40-59 / 60-79 / 80-100
TIER_BOUNDS = (0.0, 20.0, 40.0, 60.0, 80.0)

# 判定「档位真的变了」所需的迟滞余量（见 `crosses_tier_boundary`）。
#
# 病因（真机 store 里实测到的）：默认好评 20.0 **正好压在** stranger/acquainted 的分界线上，
# 只要落一个 30 秒的心跳，`apply_decay` 就把它折成 19.9998456796——分档是
# `value >= lower` 的硬比较，于是档位"变了"，`tier_change` 注入立刻触发。
# 面板显示 20.0、注入里却写着"关系档位刚刚变了"，用户看不到任何变化。
# 健康（τ=36h）与心情（τ=3h）同样会在各自的边界上出现这种亚分翻档。
#
# 为什么是 0.05：面板数值只显示一位小数，越界不足 0.05 时用户读到的数值与档名都没变，
# 对模型也就没有信息量；而它远小于任何一个真实变化（心情走完 **1 分**约需 2400 秒）——
# 迟滞只把真跨越推迟十几秒到几十秒，语义无损。
TIER_CROSSING_MARGIN = 0.05

AFFECTION_TIERS = ("stranger", "acquainted", "close", "intimate", "bonded")
MOOD_TIERS = ("sulking", "low", "calm", "happy", "elated")
HEALTH_TIERS = ("sick", "frail", "fair", "good", "vigorous")
SATIETY_TIERS = ("starving", "hungry", "satisfied", "full", "stuffed")
ENERGY_TIERS = ("exhausted", "tired", "normal", "rested", "charged")

# 顺序同时是面板展示顺序（体力/饱食在最上：它们是"今天的任务"，好感在最下：它是长期结果）
STAT_NAMES = ("energy", "satiety", "mood", "health", "affection")

_TIERS: dict[str, tuple[str, ...]] = {
    "affection": AFFECTION_TIERS,
    "mood": MOOD_TIERS,
    "health": HEALTH_TIERS,
    "satiety": SATIETY_TIERS,
    "energy": ENERGY_TIERS,
}

# ---------------------------------------------------------------------------
# 节律参数（与 `core.rhythm` 的睡眠窗一起决定"她的一天怎么过"）
#
# 这些是**模型常量**而不是配置项，与 `TIER_BOUNDS` 同一条纪律：它们是"她这个角色
# 怎么过日子"的刻画，改动属于模型改动而不是用户调参。用户能调的是
# `[our_life.decay]` / `[our_life.rhythm]` / `[our_life.economy]` 里的旋钮。
#
# 标定口径（让她像只猫，同时保证"每天要回来一次"）：
#   清醒 4.0 分/小时 + 睡眠 1.5 分/小时 × 8 小时睡眠 ⇒ 每日饱食消耗约 76 分，
#   除以一餐 38 分 ⇒ 每天约 2~3 餐（默认睡眠窗 24:00-08:00）。
#   精力：清醒 −2.2 分/小时、睡眠 +6 分/小时 ⇒ 净 +13 分/夜，正常作息下睡完是满的；
#   熬夜（23 点后不睡）额外 ×1.35 消耗，所以作息乱了她会累垮。
# ---------------------------------------------------------------------------

SATIETY_PER_HOUR_AWAKE = 4.0
SATIETY_PER_HOUR_SLEEP = 1.5

ENERGY_WAKE_DECAY_PER_HOUR = 2.2
ENERGY_SLEEP_RECOVER_PER_HOUR = 6.0
# 深夜加班系数：23:00 之后仍醒着，精力消耗额外放大（apply_decay 的 late_night 分支）
LATE_NIGHT_ENERGY_PENALTY = 1.35

# 跨轴耦合阈值：低于这些值时，对应的时间常数被放大（掉得更快 / 恢复更慢）。
# 阈值刻意落在"档"的边界上，这样面板上"她饿了"与她"心情掉得快"是同一个信号。
COUPLING_THRESHOLDS: Mapping[str, float] = {
    "satiety_low": 40.0,  # 掉进 hungry 档：心情朝向基线回落的速度 ×1.6
    "satiety_severe": 20.0,  # 掉进 starving 档：再额外 ×1.4
    "energy_low": 40.0,  # 掉进 tired 档：健康朝向基线恢复的速度 ×0.55
    "energy_severe": 20.0,  # 掉进 exhausted 档：额外 ×0.7
}

# 耦合放大系数（>1 = 该轴的时间常数变大 = 变化更快 / 恢复更慢）
COUPLING_FACTORS: Mapping[str, float] = {
    "mood_from_satiety_low": 1.6,
    "mood_from_satiety_severe": 1.4,
    "health_from_energy_low": 0.55,
    "health_from_energy_severe": 0.7,
}


@dataclass(frozen=True, slots=True)
class Stats:
    """一个角色的五项数值（未夹取；需要时调 `clamped()`）。"""

    affection: float = 20.0
    mood: float = 55.0
    health: float = 70.0
    satiety: float = 70.0
    energy: float = 80.0

    def clamped(self) -> "Stats":
        return Stats(
            affection=clamp_value(self.affection),
            mood=clamp_value(self.mood),
            health=clamp_value(self.health),
            satiety=clamp_value(self.satiety),
            energy=clamp_value(self.energy),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "affection": self.affection,
            "mood": self.mood,
            "health": self.health,
            "satiety": self.satiety,
            "energy": self.energy,
        }

    def value(self, stat: str) -> float:
        return float(getattr(self, stat))

    def tier_map(self) -> dict[str, str]:
        return {name: tier_of(name, getattr(self, name)) for name in STAT_NAMES}

    def with_value(self, stat: str, value: float) -> "Stats":
        data = self.as_dict()
        data[stat] = clamp_value(value)
        return Stats(**data)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Stats":
        """从持久化数据恢复；缺键/非法值一律回退默认（损坏数据不该让插件起不来）。

        缺键回退默认这一条同时是**向后兼容路径**：v0.1.0 的旧分片里只有三项数值，
        读进来会补上饱食/精力的默认值，旧数据不会报废也不需要迁移脚本。
        """
        base = cls()
        if not isinstance(raw, Mapping):
            return base
        out: dict[str, float] = {}
        for name in STAT_NAMES:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                out[name] = getattr(base, name)
            else:
                out[name] = clamp_value(float(value))
        return cls(**out)


def clamp_value(value: float) -> float:
    if value != value:  # NaN
        return MIN_VALUE
    return max(MIN_VALUE, min(MAX_VALUE, float(value)))


def tier_index_of(stat: str, value: float) -> int:
    """返回档位下标（0 最低）。未知 stat 抛 KeyError（调用方只应传 STAT_NAMES）。"""
    tiers = _TIERS[stat]
    bounded = clamp_value(value)
    index = 0
    for position, lower in enumerate(TIER_BOUNDS):
        if bounded >= lower:
            index = position
    return min(index, len(tiers) - 1)


def tier_of(stat: str, value: float) -> str:
    return _TIERS[stat][tier_index_of(stat, value)]


def tier_transitions(before: Stats, after: Stats) -> tuple[tuple[str, str, str], ...]:
    """哪些轴的档位发生了跨越（含升级与降级），返回 (轴, 旧档, 新档)。"""
    out: list[tuple[str, str, str]] = []
    for name in STAT_NAMES:
        old_tier = tier_of(name, getattr(before, name))
        new_tier = tier_of(name, getattr(after, name))
        if old_tier != new_tier:
            out.append((name, old_tier, new_tier))
    return tuple(out)


def axis_details(stats: Stats, *, day_start: Stats | None = None) -> dict[str, dict[str, Any]]:
    """面板「五轴卡」的明细：当前值 / 档位 / 距升档 / 今日变化（v0.6.0）。

    两条纪律（与 `core/events.py` 钉事件的同一来源）：

    - **档位线只有一个来源**：`to_next` 直接由 `TIER_BOUNDS` 相减得到，面板上
      "距下一档还差多少"与 `tier_of` 实际用的是同一条线——谁改了档位表，两处一起变，
      不存在"显示说还差 3 分、下一拍却升档了"的错位。
    - **没有锚点就说没有**：`delta_today` 只在调用方给出今日最早快照（`day_start`）时
      才算；缺锚点返回 `None`，面板整行不渲染——拿 0 冒充"今天没变"是编数据。

    `tier_index` / `next_tier` 是给面板查 `panel.tier.<stat>.<tier>` 用的稳定 ASCII 键；
    面向用户的档名由 i18n 展开，这里不出现任何中文。
    """
    out: dict[str, dict[str, Any]] = {}
    for name in STAT_NAMES:
        value = clamp_value(getattr(stats, name))
        tiers = _TIERS[name]
        index = tier_index_of(name, value)
        at_top = index + 1 >= len(tiers)
        day_value = getattr(day_start, name) if day_start is not None else None
        out[name] = {
            "value": round(value, 1),
            "tier": tiers[index],
            "tier_index": index,
            "next_tier": None if at_top else tiers[index + 1],
            "to_next": None if at_top else round(TIER_BOUNDS[index + 1] - value, 1),
            "delta_today": None if day_value is None else round(value - day_value, 1),
        }
    return out


def crosses_tier_boundary(
    stat: str, *, before: float, after: float, margin: float = TIER_CROSSING_MARGIN
) -> bool:
    """档位是否"真的"变了——**带迟滞**的比较，而不是硬比较。

    算法（`before` → `after` 是时间上相邻的两点）：

    1. 硬比较认为**没变** ⇒ 直接返回 False（绝大多数拍走这条）。
    2. 硬比较认为变了，但两点是**同一条分界线**两侧 `margin` 内的邻居 ⇒ 判为**亚分噪声**，返回 False。
    3. 其余情况 ⇒ 真跨档。

    第 2 条正是"迟滞"：每条分界线两侧各留一条 `margin` 宽的带子，两点都停在带内
    （不论谁在线上、谁在线下）就按"还在原档"处理。0.05 分的带子用户读不出来
    （面板只显示一位小数），心情走完它约需 24 秒、好感约需数月——
    语义无损，但噪声不再被当成事件。

    于是真机实测到的那对数值被吸收：

    - `20.0 → 19.9998456796`：两点都停在 20.0 那条线的带内（真机 store 里抓到的那个）；
    - `19.96 → 20.04`：同样是"贴着线抖"的一对邻居，也不会再被当成事件。

    而真走了 1 分的 `20.0 → 19.0`（面板上档名确实变了）照旧判为跨档。

    与 `tier_transitions`（面板 / 原始语义用，必须对每个数值忠实反映）分开，是因为硬比较
    会把第一种判成跨档，于是每个衰减拍都白送一次 `tier_change` 强注入
    （详见 `TIER_CROSSING_MARGIN` 的病因记录）。
    """
    old_value = clamp_value(before)
    new_value = clamp_value(after)
    old_index = tier_index_of(stat, old_value)
    new_index = tier_index_of(stat, new_value)
    if old_index == new_index:
        return False
    return not _inside_spanning_boundary_band(old_value, new_value, max(old_index, new_index), margin)


def _inside_spanning_boundary_band(
    old_value: float, new_value: float, upper_index: int, margin: float
) -> bool:
    """两点是否都停在**它们之间那条分界线**的 `margin` 带内（= 亚分噪声）。

    两点的下标不同，落点又紧贴它们中间那条线，跨越就只是噪声；否则算真事件。
    带子锚定在 `TIER_BOUNDS[upper_index]`——即两个下标中较高的那个所指的分界线，
    也就是两点**之间**那条线（两点跨过多条线时它取较高者，此时距离必然远大于 `margin`，
    于是照旧判为真事件）。
    """
    if margin <= 0.0:
        return False
    boundary = TIER_BOUNDS[upper_index]
    return abs(old_value - boundary) < margin and abs(new_value - boundary) < margin


def eventful_tier_transitions(before: "Stats", after: "Stats") -> tuple[tuple[str, str, str], ...]:
    """只保留**真事件**的跨档（噪声级的边界翻档被丢掉）。

    返回形状与 `tier_transitions` 一致 `(轴, 旧档, 新档)`；档名按**真实数值**取，
    所以注入文案里的档名与面板显示的始终一致。
    """
    out: list[tuple[str, str, str]] = []
    for name in STAT_NAMES:
        old_value = getattr(before, name)
        new_value = getattr(after, name)
        if not crosses_tier_boundary(name, before=old_value, after=new_value):
            continue
        old_tier = tier_of(name, old_value)
        new_tier = tier_of(name, new_value)
        if old_tier != new_tier:
            out.append((name, old_tier, new_tier))
    return tuple(out)


def crisis_axes(stats: Stats, inject: InjectSettings) -> tuple[str, ...]:
    """哪些轴掉进了危机档（"不高于"该档即算）。

    用「不高于」而不是「等于」：配置把阈值指到更高档时语义自然扩展，
    也不会因为一次跨越两档而漏判。
    """
    axes: list[str] = []
    for stat, configured in (
        ("mood", inject.crisis_mood_tier),
        ("health", inject.crisis_health_tier),
        ("satiety", inject.crisis_satiety_tier),
        ("energy", inject.crisis_energy_tier),
    ):
        if tier_index_of(stat, getattr(stats, stat)) <= _tier_index(stat, configured):
            axes.append(stat)
    return tuple(axes)


def is_crisis(stats: Stats, inject: InjectSettings) -> bool:
    """是否掉进危机档（心情 / 健康 / 饱食 / 精力任一"不高于"配置档位）。"""
    return bool(crisis_axes(stats, inject))


def _tier_index(stat: str, tier: str) -> int:
    try:
        return _TIERS[stat].index(tier)
    except ValueError:
        # 配置写了未知档名：退回最低档（最保守口径 = 尽量不误报危机）
        return 0


# ---------------------------------------------------------------------------
# 衰减
# ---------------------------------------------------------------------------


def _decay_toward_baseline(current: float, *, tau_hours: float, baseline: float, hours: float) -> float:
    if hours <= 0.0 or tau_hours <= 0.0:
        return current
    ratio = exp(-hours / tau_hours)
    return baseline + (current - baseline) * ratio


def _decay_toward_zero(current: float, *, tau_hours: float, hours: float) -> float:
    if hours <= 0.0 or tau_hours <= 0.0:
        return current
    return current * exp(-hours / tau_hours)


@dataclass(frozen=True, slots=True)
class CouplingSignal:
    """饱食/精力对心情与健康的放大系数（1.0 = 无影响）。"""

    mood_factor: float = 1.0
    health_factor: float = 1.0

    @property
    def hungry(self) -> bool:
        return self.mood_factor > 1.0

    @property
    def tired(self) -> bool:
        return self.health_factor < 1.0


def coupling_signal(stats: Stats) -> CouplingSignal:
    """把"饿 / 累"折算成时间常数放大系数（见模块 docstring 的跨轴耦合一节）。"""
    mood_factor = 1.0
    if stats.satiety < COUPLING_THRESHOLDS["satiety_low"]:
        mood_factor *= COUPLING_FACTORS["mood_from_satiety_low"]
    if stats.satiety < COUPLING_THRESHOLDS["satiety_severe"]:
        mood_factor *= COUPLING_FACTORS["mood_from_satiety_severe"]
    health_factor = 1.0
    if stats.energy < COUPLING_THRESHOLDS["energy_low"]:
        health_factor *= COUPLING_FACTORS["health_from_energy_low"]
    if stats.energy < COUPLING_THRESHOLDS["energy_severe"]:
        health_factor *= COUPLING_FACTORS["health_from_energy_severe"]
    return CouplingSignal(mood_factor=mood_factor, health_factor=health_factor)


def apply_coupling(stats: Stats, *, elapsed_hours: float, decay: DecaySettings) -> Stats:
    """饿了 / 累了额外带来的变化。

    语义刻意分成两半，别混：

    - **心情**：饿着时朝向基线回落得**更快**（放大后的时间常数）→ 饥一顿心情一路掉；
    - **健康**：累着时朝向基线**恢复得更慢**（缩小后的时间常数）→ 熬夜攒久了会病。

    两者都是"改变时间常数"而不是"额外加分/扣分"，所以效果是渐进的、可解释的，
    也能与 `apply_decay` 里的同一套 homeostasis 公式组合。
    """
    if elapsed_hours <= 0.0:
        return stats
    signal = coupling_signal(stats)
    if not signal.hungry and not signal.tired:
        return stats
    mood_decay = decay
    if signal.hungry:
        mood_decay = replace(decay, mood_tau_hours=max(1e-6, decay.mood_tau_hours / signal.mood_factor))
    mood = _decay_toward_baseline(
        stats.mood,
        tau_hours=mood_decay.mood_tau_hours,
        baseline=mood_decay.mood_rest_baseline,
        hours=elapsed_hours,
    )
    health = stats.health
    if signal.tired:
        health = _decay_toward_baseline(
            stats.health,
            tau_hours=max(1e-6, decay.health_tau_hours / signal.health_factor),
            baseline=decay.health_rest_baseline,
            hours=elapsed_hours,
        )
    return replace(stats, mood=clamp_value(mood), health=clamp_value(health))


def satiety_per_day(*, rhythm: "DailyRhythm | None" = None) -> float:
    """她一天掉的饱食分（清醒时段按 `SATIETY_PER_HOUR_AWAKE`、睡眠时段按更低速率）。

    口粮顾问用它推"每天几餐"（`core.economy.meal_need_per_day`），
    所以这里的口径必须与 `_satiety_points` 实际用的积分一致——两边共用同一组常量。
    """
    if rhythm is None:
        return SATIETY_PER_HOUR_AWAKE * 24.0
    return (
        rhythm.wake_hours * SATIETY_PER_HOUR_AWAKE
        + rhythm.sleep_hours * SATIETY_PER_HOUR_SLEEP
    )


def _satiety_points(*, hours: float, awake_hours: float, late_night: bool) -> float:
    """`hours` 小时里掉的饱食分；`awake_hours` 是其中醒着的小时数。"""
    if hours <= 0.0:
        return 0.0
    slept = max(0.0, hours - awake_hours)
    points = awake_hours * SATIETY_PER_HOUR_AWAKE + slept * SATIETY_PER_HOUR_SLEEP
    if late_night:
        # 深夜那一小时醒着的消耗更高（顺手把"熬夜会饿"也做出来）
        extra = awake_hours * (LATE_NIGHT_ENERGY_PENALTY - 1.0) * SATIETY_PER_HOUR_AWAKE
        points += max(0.0, extra)
    return points


def apply_decay(
    stats: Stats,
    *,
    elapsed_hours: float,
    decay: DecaySettings,
    rhythm: "DailyRhythm | None" = None,
    awake_hours: float | None = None,
) -> Stats:
    """按真实流逝时间折算五项数值（不落盘；调用方决定何时持久化）。

    - `awake_hours`：`elapsed_hours` 里她醒着的小时数。调用方用
      `core.rhythm.overlap_hours` 算出来（服务层唯一读时钟的地方）。
      不给则视为"整段都醒着"——既保证纯测试不需要时钟，也让 v0.1.0 的调用点行为不变。
    - `rhythm`：只用来判断"这段里有没有深夜时段"与拿睡眠窗；不给则退化成整天清醒。

    本函数**不做跨轴耦合**（饿/累对心情与健康的影响在 `apply_coupling` 里）：
    这样"给定钟点与时长，五项数值怎么变"与"饿了累了额外怎样"是两个可分别验证的
    纯函数，也让 `elapsed_hours=0` 严格是恒等变换（真机上的"冻结拍"依赖这一点）。

    （曾有一版在这里也按耦合系数放大时间常数，与 `apply_coupling` 叠加后健康会被
    恢复两次——`tests/test_model_rhythm_axes.py` 的门抓住了它。）
    """
    if elapsed_hours <= 0.0:
        return stats
    awake = elapsed_hours if awake_hours is None else max(0.0, min(elapsed_hours, awake_hours))
    late_night = _has_late_night(rhythm=rhythm, elapsed_hours=elapsed_hours, awake_hours=awake)

    satiety = stats.satiety - _satiety_points(hours=elapsed_hours, awake_hours=awake, late_night=late_night)
    energy = _decay_energy(
        stats.energy,
        elapsed_hours=elapsed_hours,
        awake_hours=awake,
        late_night=late_night,
    )
    return Stats(
        affection=clamp_value(
            _decay_toward_zero(stats.affection, tau_hours=decay.affection_tau_days * 24.0, hours=elapsed_hours)
        ),
        mood=clamp_value(
            _decay_toward_baseline(
                stats.mood,
                tau_hours=decay.mood_tau_hours,
                baseline=decay.mood_rest_baseline,
                hours=elapsed_hours,
            )
        ),
        health=clamp_value(
            _decay_toward_baseline(
                stats.health,
                tau_hours=decay.health_tau_hours,
                baseline=decay.health_rest_baseline,
                hours=elapsed_hours,
            )
        ),
        satiety=clamp_value(satiety),
        energy=clamp_value(energy),
    )


def _decay_energy(current: float, *, elapsed_hours: float, awake_hours: float, late_night: bool) -> float:
    """精力：清醒掉、睡眠回；深夜熬夜掉得更快。"""
    slept = max(0.0, elapsed_hours - awake_hours)
    wake_cost = awake_hours * ENERGY_WAKE_DECAY_PER_HOUR
    if late_night:
        wake_cost *= LATE_NIGHT_ENERGY_PENALTY
    return current - wake_cost + slept * ENERGY_SLEEP_RECOVER_PER_HOUR


def _has_late_night(*, rhythm: "DailyRhythm | None", elapsed_hours: float, awake_hours: float) -> bool:
    """这一段里她是否在深夜（23:00 之后）还醒着。

    没有 `rhythm` 时用"这一拍所在的小时"判断；有 `rhythm` 时直接看它的 phase——
    服务层每拍都会传一份当前时刻的 `rhythm`，所以判断与实际时钟一致。
    判据刻意宽松（只要落在深夜且醒着就算），因为熬夜惩罚本来就是"整段都算"的量级。
    """
    if awake_hours <= 0.0:
        return False
    if rhythm is not None:
        return rhythm.phase == "late_night" and not rhythm.sleeping
    return False


# ---------------------------------------------------------------------------
# 成长 / 冷落 / 吃饭
# ---------------------------------------------------------------------------


def apply_turn_gain(stats: Stats, *, session_index: int, growth: GrowthSettings) -> Stats:
    """一次用户发言的成长。`session_index` 是本次会话内第几次（0 起），用于递减收益。"""
    damp = 1.0 / (1.0 + max(0, session_index) * growth.session_diminish)
    return Stats(
        affection=clamp_value(stats.affection + growth.turn_affection_gain * damp),
        mood=clamp_value(stats.mood + growth.turn_mood_gain * damp),
        health=clamp_value(stats.health + growth.turn_health_gain * damp),
        satiety=clamp_value(stats.satiety),
        energy=clamp_value(stats.energy),
    )


def apply_judgment(stats: Stats, deltas: Mapping[str, float] | None) -> Stats:
    """把一次反馈判断的修正量加到数值上（未知轴忽略、逐轴夹取到 0..100）。

    与 `apply_turn_gain` / `apply_day_greet` 并列，但**语义完全不同**：

    - 上面两个是"互动本身就有的成长"，按发言条数给，是**基线**；
    - 这个是"她自己判断这轮怎么样"的**修正项**，有上限、需要真实互动作前提。

    所以它**不替代** `apply_turn_gain`——两者叠加，判断只是把"连发短句也能拿满步长"
    这个失真往回拉一点（`core/judgment.py` 的四条闸门保证拉不回来多少）。

    入参是已经算好的增量（不由本函数决定幅度）：幅度归 `core/judgment.judge`，
    本函数只负责"加法 + 夹取"，保持 model 层"纯算术、无策略"的职责边界。
    """
    if not deltas:
        return stats
    data = stats.as_dict()
    for name, delta in deltas.items():
        if name in data:
            data[name] = clamp_value(float(data[name]) + float(delta))
    return Stats(**data)


def apply_day_greet(stats: Stats, growth: GrowthSettings) -> Stats:
    """跨天首次互动的一次性礼物。"""
    return Stats(
        affection=clamp_value(stats.affection + growth.day_greet_affection),
        mood=clamp_value(stats.mood + growth.day_greet_mood),
        health=clamp_value(stats.health + growth.day_greet_health),
        satiety=clamp_value(stats.satiety),
        energy=clamp_value(stats.energy),
    )


def apply_anniversary(stats: Stats, *, mood: float, affection: float) -> Stats:
    """纪念日的一次性礼物（数值来自 `[our_life.anniversary]`）。"""
    return replace(
        stats,
        mood=clamp_value(stats.mood + float(mood)),
        affection=clamp_value(stats.affection + float(affection)),
    )


def apply_meal(stats: Stats, *, satiety: float, effects: Mapping[str, float] | None = None) -> Stats:
    """吃掉一份口粮：把饱食补到 `min(100, satiety + 一餐补充量)`，并叠加物品自身效果。"""
    data = stats.as_dict()
    data["satiety"] = clamp_value(float(data["satiety"]) + float(satiety))
    if effects:
        for name, delta in effects.items():
            if name in data:
                data[name] = clamp_value(float(data[name]) + float(delta))
    return Stats(**data)


def apply_item(stats: Stats, effects: Mapping[str, float] | None) -> Stats:
    """用掉一件东西：把它的效果表加到数值上（未知键忽略）。"""
    if not effects:
        return stats
    data = stats.as_dict()
    for name, delta in effects.items():
        if name in data:
            data[name] = clamp_value(float(data[name]) + float(delta))
    return Stats(**data)


def streak_milestone_bonus(
    streak_days: int, *, awarded: Iterable[int], growth: GrowthSettings
) -> tuple[int, ...]:
    """返回本次新达成的里程碑（未在 `awarded` 里且 streak 已越过），保持升序。"""
    awarded_set = {int(x) for x in awarded}
    fresh: list[int] = []
    for milestone in growth.streak_milestones:
        if streak_days >= milestone and milestone not in awarded_set:
            fresh.append(milestone)
    return tuple(sorted(fresh))


def apply_streak_bonus(
    stats: Stats, *, milestones: Iterable[int], growth: GrowthSettings
) -> Stats:
    """给若干里程碑的累计奖励（里程碑配置与奖励表按位对应，越界则忽略该位）。"""
    affection = stats.affection
    mood = stats.mood
    for milestone in milestones:
        try:
            position = growth.streak_milestones.index(milestone)
        except ValueError:
            continue
        if position < len(growth.streak_affection_bonus):
            affection += growth.streak_affection_bonus[position]
        if position < len(growth.streak_mood_bonus):
            mood += growth.streak_mood_bonus[position]
    return Stats(
        affection=clamp_value(affection),
        mood=clamp_value(mood),
        health=clamp_value(stats.health),
        satiety=clamp_value(stats.satiety),
        energy=clamp_value(stats.energy),
    )


def neglect_entitlement_days(gap_hours: float, *, grace_hours: float) -> float:
    """冷落应扣天数 = 超出宽限时长的天数（不足一天按比例）。"""
    if gap_hours <= grace_hours:
        return 0.0
    return (gap_hours - grace_hours) / 24.0


def apply_neglect(stats: Stats, *, delta_days: float, neglect: NeglectSettings) -> Stats:
    """按**增量**天数扣分（调用方用已结算天数算出增量，保证幂等）。

    各项单独夹在上限内，避免"一个月没来直接清零"这种不可逆体验。
    **只扣心情/健康/好感**：饱食与精力由作息与吃饭驱动，冷落已经不吃饭了，
    再按天额外扣一次会变成双重惩罚（`apply_decay` 已经按时间把它们带下去了）。
    """
    if delta_days <= 0.0:
        return stats
    mood_penalty = min(delta_days * neglect.mood_penalty_per_day, neglect.mood_penalty_cap)
    health_penalty = min(delta_days * neglect.health_penalty_per_day, neglect.health_penalty_cap)
    affection_penalty = min(
        delta_days * neglect.affection_penalty_per_day, neglect.affection_penalty_cap
    )
    return Stats(
        affection=clamp_value(stats.affection - affection_penalty),
        mood=clamp_value(stats.mood - mood_penalty),
        health=clamp_value(stats.health - health_penalty),
        satiety=clamp_value(stats.satiety),
        energy=clamp_value(stats.energy),
    )


def advance_streak(
    last_active_date: str | None, today: str, current_streak: int
) -> tuple[int, bool, bool]:
    """把连续互动天数结算到 `today`。

    返回 `(新的 streak、是否今天首次互动、是否断档)`：

    - `last_active_date == today` → 今天已经来过：streak 原样，`is_new_day=False`
      （跨天礼物不会重复给）
    - `last_active_date == today 的前一天` → streak + 1，`is_new_day=True`
    - 其它（更早 / 为空 / 非法日期）→ streak 重置为 1，`is_new_day=True`，断档标记依据是否曾有记录
    """
    if not today:
        return max(1, current_streak), False, False
    if last_active_date == today:
        return max(1, current_streak), False, False
    if _previous_day(today) == last_active_date:
        return max(1, current_streak) + 1, True, False
    return 1, True, bool(last_active_date)


def _previous_day(day: str) -> str | None:
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return None
    return (parsed - timedelta(days=1)).isoformat()


def as_day_seconds(hours: float) -> float:
    """小时 → 秒的换算（口语化的配置项换算用得上）。"""
    return float(hours) * 3600.0
