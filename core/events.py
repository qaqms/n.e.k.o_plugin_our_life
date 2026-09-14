"""阶段性事件：把"她身上真的发生了什么"识别成可命名的一次性事件。

这一层存在的理由（v0.4.0「阶段性事件与面板叙事」主线）：

v0.3.0 之前，数值只会**连续地**动，她的话也只有一种口吻——"我现在处于某档"。
于是有一段体验是缺失的：**跨越那两个最坏的档位、终于好起来的那一刻，没有一个名字**。
`tier_change` 会如实播报"身体：生病 → 一般"，但那仍是**读数**，不是**事件**；
面板上也只看得见"现在是什么档"，看不见"她病过、然后好了"。

这一层把那种时刻从连续读数里**摘出来**，给它一个稳定的名字（`sick_recovery` /
`cheered_up`），供注入层写成一句叙事、供面板列成一条经历。

## 四个设计决定（都写在这里，防后人当成疏漏）

### 1. 判据挂在**档位下界**上，不挂可配置的危机阈值

健康从「生病 / 虚弱」（下界 0 / 20）升到 **40** 及以上 = `sick_recovery`；
心情从「闹脾气」（下界 0）升到 **20** 及以上 = `cheered_up`。

这两个数字不是新拍的：它们是 `core/model.TIER_BOUNDS` 里的两个元素，
模块 import 时就用 `_self_check()` 钉住（谁动了档位表，这里立刻炸而不是悄悄错位）。
于是"面板显示她脱离了生病档"与"插件认为她病好了"永远是同一个判据——
与 `core/rhythm.ANNIVERSARY_DAYS` 被刻意排除在配置之外是同一条纪律。

刻意**不用** `crisis_axes` 做判据：那是**可配置**的危机阈值（`[our_life.inject].crisis_*`），
用户把它调高调低会连带改变"什么算病好了"——而"病好了"这件事不该随注入阈值漂移。

### 2. 心情恢复只认「闹脾气 → 低落及以上」，不认更浅的档

心情是**小时级**（τ=3h，向基线 55 回落），一天里本来就会跨好几次档。
把 `低沉 → 平静` 也算成事件的话，她一天能"振作"五六回，词就贬值了。
所以只认**脱离最坏那一档**（`sulking`，下界 0）——那才是"哄好了"。

### 3. 同一件事有冷却，且台账跨天不过期

`mood_recovery_min_interval_hours`（默认 6h）压住"掉下去又上来"的反复；
`health_recovery_min_interval_hours`（默认 20h）比一天略短，允许**隔天再次生病又康复**
（那是真的两次经历），但压住同一天里的抖动。

台账（`ShardState.event_history`）只存**事件名、时刻与当时的数值快照**，
不存任何对话正文——与反馈闭环台账同一条隐私纪律（见 `services/state.py`）。

### 4. 睡眠只抑制**注入**，不抑制**识别**

凌晨三点她病好了，这件事**发生过**，面板上就该有一条；但不该把她吵醒
（与 `services/injector.py` 的睡眠静默同一套判断）。所以本层总是返回事件，
由调用方拿 `StagedInjection.suppressed` 决定发不发。这条分开很重要：
"要不要记"与"要不要说"是两个问题，混在一起会让睡过去的事件被下一拍重新识别一遍。

## 本轮刻意**不做**的

- ❌ **生日**：需要"角色的生日"这个新配置面与用户的真实输入，属于独立一轮
  （路线图里与代餐成长、情绪感知并列）。本轮不动注入层既有的纪念日触发器。
- ❌ **给事件加数值奖励**：纪念日已经会给礼物（`apply_anniversary`），
  再给事件配奖励会让"康复"变成刷数值的路径。事件**只叙事**，不碰数值——
  这也让本层保持纯函数、可确定性单测。
- ❌ **为事件开第三条频控**：事件仍复用既有注入链路与频控
  （见 `services/injector.py` 的 `plan_for_event`），所以 `max_per_hour` /
  睡眠静默两道闸门对它一样生效，不需要在频控上开口子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .configuration import EventSettings
from .model import TIER_BOUNDS, Stats

__all__ = [
    "EVENT_CHEERED_UP",
    "EVENT_KEYS",
    "EVENT_SICK_RECOVERY",
    "HEALTH_RECOVERY_LINE",
    "MOOD_RECOVERY_LINE",
    "StagedEvent",
    "event_already_fired",
    "pick_event",
]

# 稳定 ASCII 事件名（面向用户的文案走 i18n `panel.event.<键>`）。
EVENT_SICK_RECOVERY = "sick_recovery"
EVENT_CHEERED_UP = "cheered_up"

# 判定顺序 = 优先级顺序（越靠前越优先）。
# 病愈排在哄好之前：从"生病"里出来是更重的一件事，而且它通常也伴随心情回升，
# 同拍只报一条时不该被较轻的那条挤掉。
EVENT_KEYS: tuple[str, ...] = (EVENT_SICK_RECOVERY, EVENT_CHEERED_UP)

# 两条"好起来了"的线，直接引用档位表（单一来源，见模块 docstring 第 1 条）。
#   健康档：sick(0-20) 与 frail(20-40) **都**读作"她病着"（面板：生病 / 虚弱），
#           所以"病愈"要跨过 40 才算真的好了。
#   心情档：只有 sulking(0-20) 是"她闹脾气"（low 已是"低落"，能哄了），跨过 20 即可。
HEALTH_RECOVERY_LINE: float = TIER_BOUNDS[2]
MOOD_RECOVERY_LINE: float = TIER_BOUNDS[1]


def _self_check() -> None:
    """把两条线钉在档位表上（谁改了 `TIER_BOUNDS`，import 时立刻炸，而不是悄悄错位）。"""
    if HEALTH_RECOVERY_LINE != TIER_BOUNDS[2]:
        raise AssertionError("TIER_BOUNDS[2] moved: 'sick' band upper bound changed")
    if MOOD_RECOVERY_LINE != TIER_BOUNDS[1]:
        raise AssertionError("TIER_BOUNDS[1] moved: 'sulking' band upper bound changed")


_self_check()


@dataclass(frozen=True, slots=True)
class StagedEvent:
    """一次被认定发生的阶段性事件（纯数据，不含任何文案）。

    `wake_ok` = 这条是否允许在**她睡觉时**仍然注入。
    只有"从生病里出来"给了 True：那是危机**解除**的通知，晚八小时说就没有意义了；
    而"哄好了"是好消息，凌晨三点把她叫醒说这个与插件的"过日子"调子相反
    （与 `services/injector.py` 的睡眠静默同一套判断）。

    `width` = 这次跨越的幅度（脱离最坏档时超出去多少分），只用于面板排序与调试，
    不进注入正文——注入契约是不给模型看原始数字（`core/injection.py` 模块 docstring）。
    """

    key: str
    stat: str
    value: float
    width: float
    wake_ok: bool

    def as_dict(self, *, at: float) -> dict[str, object]:
        """台账条目（只含事件名/轴/时刻与数值快照，**不含任何正文**）。"""
        return {
            "key": self.key,
            "stat": self.stat,
            "value": round(self.value, 2),
            "width": round(self.width, 2),
            "at": at,
        }


def _crossed_up(before: float, after: float, line: float) -> bool:
    """`after` 刚跨到线上、而 `before` 还在线下（开区间）。

    两个边界细节：
    - `after >= line` 用闭区间：面板显示 40.0 时档位就已经是"一般"了，判据必须跟着它；
    - `before < line` 用开区间：一直停在线上不重复触发（`before == line` 不算"刚跨过"）。
    """
    return before < line <= after


def pick_event(
    *,
    before: Stats,
    after: Stats,
    settings: EventSettings,
    now: float,
    ledger: Iterable[Mapping[str, object]] = (),
) -> StagedEvent | None:
    """挑出这一拍最重要的那件阶段性事件；没有就返回 None（绝大多数拍都是 None）。

    `ledger` 传 `ShardState.event_history`（最近若干条 `{key, at, ...}`），用来判冷却。
    台账是**有界**的（`services/state.EVENT_HISTORY_MAX`），所以冷却窗口必须短于
    "台账能覆盖的时间跨度"，否则老条目被挤掉后冷却就失效了——
    默认值下最坏情况是 6h 冷却 vs 12 条台账，一天最多 4 条，余量充足。

    本函数**不管睡眠**：睡眠只影响"发不发"，不影响"算不算发生过"
    （见模块 docstring 第 4 条）。调用方拿到事件后无条件记台账，
    再由 `services/injector` 按 `wake_ok` 与作息决定是否真的注入。
    """
    if not settings.enabled:
        return None

    candidates: list[StagedEvent] = []

    if _crossed_up(before.health, after.health, HEALTH_RECOVERY_LINE):
        candidates.append(
            StagedEvent(
                key=EVENT_SICK_RECOVERY,
                stat="health",
                value=after.health,
                width=after.health - HEALTH_RECOVERY_LINE,
                wake_ok=True,
            )
        )

    if _crossed_up(before.mood, after.mood, MOOD_RECOVERY_LINE):
        candidates.append(
            StagedEvent(
                key=EVENT_CHEERED_UP,
                stat="mood",
                value=after.mood,
                width=after.mood - MOOD_RECOVERY_LINE,
                wake_ok=False,
            )
        )

    for event in candidates:
        # 冷却检查**先于**一切：睡过去的事件也已经记了台账，
        # 所以下一拍走到这里时它本来就在冷却里，不会被重复识别。
        if _on_cooldown(event=event, settings=settings, now=now, ledger=ledger):
            continue
        return event

    return None


def _on_cooldown(
    *,
    event: StagedEvent,
    settings: EventSettings,
    now: float,
    ledger: Iterable[Mapping[str, object]],
) -> bool:
    """同一件事在冷却窗口内不再重复。

    只按 `key` 比，不按轴比——"她又病好了"这件事由 `key` 定义，
    而不是由"哪一根轴动了"定义。
    """
    hours = (
        settings.health_recovery_min_interval_hours
        if event.key == EVENT_SICK_RECOVERY
        else settings.mood_recovery_min_interval_hours
    )
    if hours <= 0.0:
        return False
    window = hours * 3600.0
    for entry in ledger:
        if not isinstance(entry, Mapping) or entry.get("key") != event.key:
            continue
        at = entry.get("at")
        if isinstance(at, (int, float)) and (now - float(at)) < window:
            return True
    return False


def event_already_fired(
    *,
    key: str,
    ledger: Iterable[Mapping[str, object]],
    now: float,
    within_seconds: float,
) -> bool:
    """台账里最近 `within_seconds` 内是否已有同一个事件。

    与 `_on_cooldown` 分开导出，是因为调用方有时只想**读**有没有发生过
    （例如面板显示"今天病愈过一次"），而不想触发冷却判定。
    """
    if within_seconds <= 0.0:
        return False
    for entry in ledger:
        if not isinstance(entry, Mapping) or entry.get("key") != key:
            continue
        at = entry.get("at")
        if isinstance(at, (int, float)) and (now - float(at)) < within_seconds:
            return True
    return False
