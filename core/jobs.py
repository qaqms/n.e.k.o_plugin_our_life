"""猫娘打工（v0.7.0）：班次目录、开工判据与结算——纯函数层，零 SDK 依赖。

「用时间换金钱」的拟真口径：

1. **表列消耗是额外损耗**。班次进行期间，`core/model` 的自然衰减照常走
   （上班两小时精力本来就掉 ~4.4 分）——她不会因为你在打工就暂停变累。
   目录里的 `energy_cost` 等是这份**劳动本身**的额外磨损，结算时一次扣清。
   若把自然衰减停掉再扣表值，等于"上班比躺着养精神"，拟真就反了。
2. **工钱按结算时刻的状态打折**（`wage_factor`）：饿着肚子/带着病上班，
   下班拿到的钱会少一截。因果读得出来，而不是凭空 -5。
3. **班次是持久化的真实时间**：`job_end_at` 存进分片，进程重启、电脑关机都
   不影响"到点结算"——tick 下一拍看到 `now >= job_end_at` 就把这班结掉。
   这与惰性衰减同一手法，没有常驻计时器。
4. **睡眠窗不可排班**：整段班次（起 → 止）都不许压到睡眠窗上，
   判据逐时走 `core/rhythm.is_sleep_hour`，跨零点自动正确。
   中途醒来（早退）另按 `early_leave_ratio` 打折。
5. **目录固定在代码里**（与 `core/economy.ITEMS` 同一纪律）：小时数、工钱、
   消耗、门槛是"她是怎么过日子的"的刻画，属模型常量；配置只留
   `enabled / max_shifts_per_day / early_leave_ratio` 三个行为旋钮。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .model import MAX_VALUE, Stats, clamp_value
from .rhythm import is_sleep_hour, normalize_hour

__all__ = [
    "JOBS",
    "JOB_IDS",
    "JOB_NARRATION_ZH",
    "JOB_ORDER",
    "MAX_SHIFTS_PER_DAY",
    "WAGE_FACTOR_CEIL",
    "WAGE_FACTOR_FLOOR",
    "Job",
    "JobOutcome",
    "apply_shift_costs",
    "job",
    "job_catalog",
    "job_narration",
    "requirements_line",
    "settle_shift",
    "shift_hits_sleep_window",
    "start_block_reason",
    "wage_factor",
    "wage_preview",
]

# 每日班次上限的代码侧天花板（配置夹到这里，防止把上限调成印钞机）
MAX_SHIFTS_PER_DAY = 3

# 工钱系数区间：结算时刻身心越好越接近 ceil，越差越接近 floor
WAGE_FACTOR_FLOOR = 0.8
WAGE_FACTOR_CEIL = 1.1


@dataclass(frozen=True, slots=True)
class Job:
    """一份工作。id 是稳定 ASCII 键；面向用户的名称走 i18n `panel.job.<id>`。

    `costs` 是**额外损耗**（自然衰减之外的那一截），结算时一次作用；
    `requires` 是开工门槛（按开工时刻的数值判定，不看结算）。
    """

    id: str
    label_zh: str
    hours: float
    base_wage: int
    costs: tuple[tuple[str, float], ...]
    requires: tuple[tuple[str, float], ...] = ()
    order: int = 0


# 班次目录（单一来源）。标定：全部班次打满 = 每日 +~150（慢速档，且要拿属性去换——
# 换来的损耗会推高口粮/药品需求，形成"赚的钱又花回她身上"的闭环）。
_JOB_TABLE: tuple[Job, ...] = (
    Job(
        id="konbini",
        label_zh="便利店收银",
        hours=2.0,
        base_wage=25,
        costs=(("energy", 10.0), ("satiety", 8.0), ("mood", 4.0)),
        order=10,
    ),
    Job(
        id="mascot",
        label_zh="咖啡店吉祥物",
        hours=3.0,
        base_wage=40,
        costs=(("energy", 14.0), ("satiety", 10.0), ("mood", 8.0)),
        requires=(("energy", 35.0),),
        order=20,
    ),
    Job(
        id="night_market",
        label_zh="夜市摆摊",
        hours=4.0,
        base_wage=60,
        costs=(("energy", 22.0), ("satiety", 16.0), ("mood", 12.0)),
        requires=(("energy", 50.0), ("satiety", 40.0)),
        order=30,
    ),
)

JOBS: Mapping[str, Job] = {entry.id: entry for entry in _JOB_TABLE}
JOB_IDS: tuple[str, ...] = tuple(entry.id for entry in _JOB_TABLE)
JOB_ORDER: tuple[str, ...] = tuple(
    entry.id for entry in sorted(_JOB_TABLE, key=lambda entry: (entry.order, entry.id))
)


def job(job_id: str) -> Job | None:
    return JOBS.get(job_id) if isinstance(job_id, str) else None


# 下班叙事模板（注入文本固定中文，与 `core/injection.EVENT_NARRATION_ZH` 同一口径）。
# {pay} 是这班挣到的金币——金币不是五项状态那类"不该被念出来的读数"，
# 而是她今天挣到的事实，她当然知道自己带回了多少钱。
JOB_NARRATION_ZH: Mapping[str, str] = {
    "konbini": "你在便利店站了 {hours:g} 个小时的班，收银台后腿都站酸了；{pay} 枚金币到账，心里踏实",
    "mascot": "你穿着玩偶服在店里招呼了 {hours:g} 个小时的客人，闷热，但有人因为你笑了一下；{pay} 枚金币到账",
    "night_market": "你在夜市摆了 {hours:g} 个小时的摊，烟火气熏得人累并快乐着；{pay} 枚金币带回了家",
}


def job_narration(job_id: str, *, pay: int, early: bool) -> str:
    """结算叙事行（注入正文用）。未知工作返回空串；早退换一种说法。"""
    found = job(job_id)
    if found is None:
        return ""
    template = JOB_NARRATION_ZH.get(job_id, "你打了 {hours:g} 个小时的工，带回来 {pay} 枚金币")
    line = template.format(hours=found.hours, pay=int(pay))
    if early:
        line = line + "——虽然中途就被叫回来了，这趟还是挣的"
    return line


# ---------------------------------------------------------------------------
# 工钱
# ---------------------------------------------------------------------------


def wage_factor(stats: Stats) -> float:
    """结算时刻的身心状态 → 工钱系数（线性，端点封顶）。

    取精力/心情/健康三项的均值：三者都低说明她是在硬撑，这班挣得的钱要打折。
    刻意不含饱食与好感——饱食低是"该吃饭了"（钱本来就少），再打一次折是双重惩罚；
    好感是长期量，不该在时薪这种短期结算里出现。
    """
    mean = (stats.energy + stats.mood + stats.health) / 3.0
    position = max(0.0, min(1.0, mean / MAX_VALUE))
    return WAGE_FACTOR_FLOOR + (WAGE_FACTOR_CEIL - WAGE_FACTOR_FLOOR) * position


def wage_preview(job_id: str) -> tuple[int, int] | None:
    """工钱区间（面板展示用）：floor/ceil 系数下的两端，让"值不值"看得见。"""
    found = job(job_id)
    if found is None:
        return None
    low = int(round(found.base_wage * WAGE_FACTOR_FLOOR))
    high = int(round(found.base_wage * WAGE_FACTOR_CEIL))
    return (low, high)


def requirements_line(job_id: str) -> dict[str, float]:
    """开工门槛（拷贝）；未知工作返回空表。"""
    found = job(job_id)
    return {} if found is None else {name: value for name, value in found.requires}


# ---------------------------------------------------------------------------
# 睡眠窗判据
# ---------------------------------------------------------------------------


def shift_hits_sleep_window(
    *,
    start: datetime,
    hours: float,
    sleep_start_hour: int,
    sleep_end_hour: int,
) -> bool:
    """整段班次是否压到睡眠窗（逐小时推进判整点窗，与 `overlap_hours` 同一粒度）。

    判"起点 + 每一个中间整点"：班次哪怕只有最后一个小时落进睡眠窗，也视为冲突——
    她不该在睡着的时间上班，哪怕只睡一分钟。
    """
    if hours <= 0.0:
        return False
    total_hours = int(hours * 60)
    cursor_minutes = start.hour * 60 + start.minute
    for step in range(0, total_hours + 1, 30):
        minute_of_day = (cursor_minutes + step) % (24 * 60)
        if is_sleep_hour(
            normalize_hour(minute_of_day // 60),
            sleep_start_hour=sleep_start_hour,
            sleep_end_hour=sleep_end_hour,
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# 开工判据
# ---------------------------------------------------------------------------


def start_block_reason(
    *,
    job_id: str,
    stats: Stats,
    active_job_id: str,
    shifts_today: int,
    max_per_day: int,
    sleeping: bool,
    hits_sleep_window: bool,
) -> str:
    """可否开工：返回 `"ok"` 或稳定错误码（进 codes.py 契约）。

    判据顺序即优先级：先问"在不在班"（最直观），再问今日额度与作息，
    最后才问身体条件——把 `job_invalid` 放最前，因为它连对象都不存在。
    """
    if job(job_id) is None:
        return "invalid_job"
    if active_job_id:
        return "already_working"
    if sleeping or hits_sleep_window:
        return "job_sleep_window"
    if shifts_today >= max(0, int(max_per_day)):
        return "job_daily_limit"
    found = job(job_id)
    assert found is not None  # 上面已挡
    for stat_name, minimum in found.requires:
        if stats.value(stat_name) < minimum:
            return "job_needs_rest"
    return "ok"


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """一次班次结算（早退与正常下班共用；`fraction` 是完成比例）。"""

    job_id: str
    pay: int
    fraction: float
    costs: tuple[tuple[str, float], ...]
    early: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job_id,
            "pay": self.pay,
            "fraction": round(self.fraction, 4),
            "costs": [[name, delta] for name, delta in self.costs],
            "early": self.early,
        }


def settle_shift(
    *,
    job_id: str,
    now: float,
    started_at: float,
    end_at: float,
    stats: Stats,
    early_leave_ratio: float,
) -> JobOutcome | None:
    """将班次按已完成的**时间比例**结算。返回 None = 无进行中班次/数据不合法。

    - 正常到点（`now >= end_at`）：fraction = 1；
    - 早退（调用方在 `now < end_at` 时来结）：fraction = 已耗时/总时长，
      工钱再乘 `early_leave_ratio`（提前收工挣得更少是经济纪律，不是惩罚），
      额外损耗**按时长比例**扣——干一半的活只磨损一半。
    - 结算作用于调用方传入的 `stats`？不：本函数只**报告**要扣的消耗与要发的工钱，
      改数值由调用方用 `apply_shift_costs` 做——与 `meal_plan`（选哪件）
      / `apply_meal`（吃下去）同一分层，纯函数不偷偷改参数。
    """
    found = job(job_id)
    if found is None:
        return None
    span = max(1e-6, end_at - started_at)
    fraction = max(0.0, min(1.0, (now - started_at) / span))
    early = fraction < 1.0
    factor = wage_factor(stats)
    pay = found.base_wage * factor * fraction
    if early:
        pay *= max(0.0, min(1.0, float(early_leave_ratio)))
    costs = tuple((name, round(delta * fraction, 2)) for name, delta in found.costs)
    return JobOutcome(
        job_id=found.id,
        pay=max(0, int(round(pay))),
        fraction=fraction,
        costs=costs,
        early=early,
    )


def apply_shift_costs(stats: Stats, outcome: JobOutcome) -> Stats:
    """把结算报告里的额外损耗作用到数值上（逐项夹取，NaN 安全由 clamp 兜）。"""
    values = stats.as_dict()
    for name, delta in outcome.costs:
        if name in values:
            values[name] = clamp_value(values[name] - delta)
    return Stats(**values)


# ---------------------------------------------------------------------------
# 面板目录视图
# ---------------------------------------------------------------------------


def job_catalog() -> list[dict[str, Any]]:
    """货架式的工作目录（面板直接渲染；数字全部来自本模块的单一来源）。

    今日次数/上限不塞进这里：目录是**静态真相**，计数是**当次读数**，
    两层在 context 装配处合流（与商店货架 `_shop_catalog` 同一分工）。
    """
    out: list[dict[str, Any]] = []
    for entry_id in JOB_ORDER:
        found = JOBS[entry_id]
        preview = wage_preview(entry_id)
        out.append(
            {
                "id": found.id,
                "label_zh": found.label_zh,
                "hours": found.hours,
                "base_wage": found.base_wage,
                "pay_low": preview[0] if preview else 0,
                "pay_high": preview[1] if preview else 0,
                "costs": [[name, delta] for name, delta in found.costs],
                "requires": [[name, value] for name, value in found.requires],
            }
        )
    return out
