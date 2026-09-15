"""「她此刻的状态」页的判据层（v0.8.0）：把数值折算成**面向主人的状态档案**。

纯函数层——不导入任何 `plugin.sdk.*`，不读时钟也不落盘，全部输入由调用方给。

## 为什么这一层存在，而不是让面板自己算

面板拿到的原料本来就在 context 里（`axes` / `runtime` / `state`），但"她此刻该说什么"
是一条**判据**：挑哪几根轴开口、危机时多说几句、哪两条轴正在互相拖累。判据放进 TSX
就会出现"面板上写的和她注入时说的不是一回事"，而且这一层没有任何测试门能管住它。
所以这里只出**判据与键名**，一个字面的中/英文案都不产——文案全在 i18n，
呈现全在 `ui/components/state_page.tsx`。

## 与 `core/injection.build_text` 是平行出口，不是复用

两者读同一张档位表，但受众相反：

| | `build_text`（注入） | 本模块（面板） |
|---|---|---|
| 写给谁 | 模型（第二人称"你"=她） | 主人（第一人称"我"=她） |
| 能不能出现数字 | ❌ `_FOOTER` 明令禁止 | ✅ 档案段就是数值明细 |
| 内容 | 感受 + 该怎么做（25 句行为提示） | 感受 + 她的一天（无指令） |
| 文案存放 | 本模块内的中文字面量 | i18n `panel.stateVoice.*`（双语） |

复用会把两条语义搅在一起：注入那句"你可以直接说饿，但别演成讨饭"是**给模型的指令**，
摊到面板上就是一段出戏的旁白。所以两套模板、一张档位表。

## 三条不许越过的线

1. **只产已登记的键**：`voice` 里每一项都是 `panel.stateVoice.<轴>.<档>`，轴取自
   `STAT_NAMES`、档取自 `_TIERS`，两者都由 `tests/test_i18n_contract.py` 的结构门
   逐格钉住。拼不出合法档位（脏数据）时**丢掉那一句**，而不是造一个新键。
2. **整句，不逐词拼**：本模块只给键名数组，连接由前端按语序处理。中英词序不同，
   任何"主语键 + 谓语键"的拼法在 en 下必坏（forever_companion 第九轮的同一课）。
3. **缺证据就说缺**：没有锚点的 `delta_today`、没算出来的 `gap_hours` 一律 `None`，
   面板整行不渲染。拿 0 冒充"今天没变"是编数据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .judgment import JUDGMENT_LABELS
from .model import (
    AFFECTION_TIERS,
    COUPLING_FACTORS,
    ENERGY_TIERS,
    HEALTH_TIERS,
    MOOD_TIERS,
    SATIETY_TIERS,
    STAT_NAMES,
)
from .rhythm import DailyRhythm

if TYPE_CHECKING:  # 只为类型标注，运行期靠鸭子类型取值（与 model.py 同一条依赖方向）
    from .model import CouplingSignal

__all__ = ["COUPLING_CODES", "build_state_note", "coupling_codes", "voice_keys"]

# 轴 -> 合法档位序列。从 `core.model` 的档位表**引用**而来，不在这里重抄一份：
# 改档位表时这里自动跟着走，不会出现"面板认得旧档名、判据用新档名"。
_TIERS_BY_STAT: Mapping[str, tuple[str, ...]] = {
    "affection": AFFECTION_TIERS,
    "mood": MOOD_TIERS,
    "health": HEALTH_TIERS,
    "satiety": SATIETY_TIERS,
    "energy": ENERGY_TIERS,
}

# 自述里"中性档"的下标（0-4 里的 2）。低于它就是"坏事"，高于它就是"好事"。
_NEUTRAL_TIER_INDEX = 2

# 句数随等级变动的三条线（用户确认过的口径）：
# 危机 → 3 句；有任一项掉进"偏低"（档 0/1）→ 2 句；全都还行 → 1 句。
_VOICE_COUNT_CRISIS = 3
_VOICE_COUNT_LOW = 2
_VOICE_COUNT_STEADY = 1

# 低于（含）这个档下标就算"偏低"——即 TIER_BOUNDS 的 0/1 两档：累垮了 / 有点累。
_LOW_TIER_INDEX = 1

# "今天的日子"四根轴：能把她拉进"偏低→多说一句"的，只有这四根。
# 好感**不在**里面：它的**默认值就是 20.0**（`Stats.affection`），新角色一装好就坐在
# `acquainted`（档 1）。若把它算作升级判据，每个新角色都会永远被当成"处于偏低"，
# 天天多一句"我还不太敢跟你撒娇"——那不是身体不对劲，那是**刚开始**。
# 与 `core/jobs.py` 的"好感是长期量，不进时薪"是同一条纪律。
_DAILY_AXES = ("energy", "satiety", "mood", "health")

# 坏消息优先于好消息：一档之差的权重远高过"她状态最好的一面"，
# 所以排序键是 `bad * _BAD_WEIGHT - good`。乘权是为了让"往下掉两档"永远压过"往上高两档"。
_BAD_WEIGHT = 10

# 耦合放大系数的比较容差：因子由常量相乘得到（1.6 × 1.4 = 2.2400000000000002），
# 用 `>=` 直接比会因浮点尾巴漏判，所以留一个远小于任何真实间距的余量。
_FACTOR_EPS = 1e-9

# 耦合因子的分档线：**从常量表算出来**，不写死数字。
# 谁调了 `COUPLING_FACTORS`，这里的"重度"判据自动跟着走（与 judgment 闸门 ① 同一手法）。
_MOOD_FACTOR_MILD = COUPLING_FACTORS["mood_from_satiety_low"]
_MOOD_FACTOR_SEVERE = _MOOD_FACTOR_MILD * COUPLING_FACTORS["mood_from_satiety_severe"]
_HEALTH_FACTOR_MILD = COUPLING_FACTORS["health_from_energy_low"]
_HEALTH_FACTOR_SEVERE = _HEALTH_FACTOR_MILD * COUPLING_FACTORS["health_from_energy_severe"]

# 耦合码的**全集**：`coupling_codes` 只会从这里取子集，i18n 结构门也按这里逐项钉文案。
# 写成常量而不是让测试自己拼，是为了与 `PANEL_ERROR_CODES` / `ITEM_IDS` / `JOB_IDS` 同一个形状：
# **判据与文案共用一个来源**，谁新加一档耦合而忘写文案，门必红。
COUPLING_CODES: tuple[str, ...] = (
    "mood_from_satiety",
    "mood_from_satiety_severe",
    "health_from_energy",
    "health_from_energy_severe",
)


def _tier_axis(axes: Mapping[str, Mapping[str, Any]], stat: str) -> tuple[str, int, float] | None:
    """取一根轴的 (档位键, 档下标, 数值)；原料不齐就返回 `None`（绝不补造）。

    档位键只认 `_TIERS_BY_STAT[stat]` 里的合法值：`axes` 是 `axis_details` 现算的，正常永远合法；
    这里的收窄是给**脏 context / 未来改表**兜底的——宁可少说一句，也不产一个 i18n 里没有的键。
    """
    raw = axes.get(stat)
    if not isinstance(raw, Mapping):
        return None
    tier = raw.get("tier")
    if not isinstance(tier, str) or tier not in _TIERS_BY_STAT[stat]:
        return None
    try:
        index = int(raw.get("tier_index"))
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= len(_TIERS_BY_STAT[stat]):
        return None
    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        return None
    return tier, index, value


def _salience(index: int, value: float) -> tuple[float, float]:
    """排序键：(离中性档的距离, 同距离时的次级序)。

    - 掉在中性档以下：**离中性越远越靠前**（`bad = 2 - index`，乘 `_BAD_WEIGHT` 压过一切好消息）。
    - 全都在中性档以上：反过来，**她挑自己最好的一面说**（`good = index - 2`）——
      平稳的一天没必要让她先汇报"还行"。

    次级序（同距离时）才是数值：低档取"越低越靠前"，高档取"越高越靠前"。
    注意这一条排在 `STAT_NAMES` 次序**之后**生效（见 `voice_keys`），理由见那里的注释。
    """
    if index < _NEUTRAL_TIER_INDEX:
        return (float(_NEUTRAL_TIER_INDEX - index) * _BAD_WEIGHT, -value)
    return (float(index - _NEUTRAL_TIER_INDEX), value)


def _voice_count(axes: Mapping[str, Mapping[str, Any]], crisis: bool) -> int:
    """自述句数随等级变动（用户口径：危机 3 句 / 偏低 2 句 / 平稳 1 句）。

    危机直接看调用方传进来的 `crisis`（`core.model.is_crisis` 的判据，它的口径本来就
    只含心情/健康/饱食/精力四根轴）。"偏低"也只查 `_DAILY_AXES`（见常量注释）：
    好感低不是"今天的毛病"，不该把句数推高、也不该让她每天先为这个开口。

    上限同时是**可用句数**：原料只有一根轴时不会硬凑出三句（见 `voice_keys` 的截断）。
    """
    if crisis:
        return _VOICE_COUNT_CRISIS
    for stat in _DAILY_AXES:
        axis = _tier_axis(axes, stat)
        if axis is not None and axis[1] <= _LOW_TIER_INDEX:
            return _VOICE_COUNT_LOW
    return _VOICE_COUNT_STEADY


def voice_keys(
    axes: Mapping[str, Mapping[str, Any]],
    *,
    crisis: bool = False,
    crisis_axes: Sequence[str] = (),
) -> list[str]:
    """她要开口说的这几句是什么：返回**整句 i18n 键**的有序列表（1~3 项）。

    顺序有三层，缺一不可：

    1. **危机档最先说**（`crisis_axes` 是 `core.model.crisis_axes` 的判据结果，不在这层重算）。
    2. 然后按"离中性档的距离"——她先说最不对劲的那一根轴。
    3. **同距离时按 `STAT_NAMES` 的固定顺序**（精力/饱食在前，好感在后），数值只作最后兜底。

    第 3 层不是审美问题，是正确性问题：`affection` 的**默认值就是 20.0**，新角色一装好
    就坐在 `acquainted`（档 1，属于"低于中性"）。若让数值压过展示顺序，一个刚认识主人的
    角色会永远把"我还不太敢跟你撒娇"排在"我有点累"前面——把长期结果当成今天的毛病报。
    `STAT_NAMES` 的注释早就写明"它们是今天的任务 / 好感是长期结果"，这里就是那条纪律的落地。

    只产合法档位键：`_tier_axis` 已经把认不出的档名收成 `None` 并丢弃，所以这里拼出的
    每一个键都在 i18n 结构门的射程里（少一句好于喷一个空白译名）。

    这里不产文案也不产参数：需要数字的句子（"距升档还差多少"）在身体段，自述段刻意
    不带数字——她不会用"我饱食 42 分"这种话说自己。
    """
    crisis_set = set(crisis_axes)
    # 元组逐项升序排，所以"越靠前"的字段全部取负；第 3 位是 STAT_NAMES 次序，
    # 第 4 位才是数值次级序（已由 `_salience` 翻成"越大越该靠前"，故取负）。
    ranked: list[tuple[int, float, int, float, str, str]] = []
    for position, stat in enumerate(STAT_NAMES):
        axis = _tier_axis(axes, stat)
        if axis is None:
            continue
        tier, index, value = axis
        salience_a, salience_b = _salience(index, value)
        ranked.append((0 if stat in crisis_set else 1, -salience_a, position, -salience_b, stat, tier))
    ranked.sort()
    limit = _voice_count(axes, crisis)
    return [f"panel.stateVoice.{item[4]}.{item[5]}" for item in ranked[:limit]]


def coupling_codes(coupling: "CouplingSignal | None") -> list[str]:
    """现在哪两根轴正在拖累别处：返回稳定 ASCII 码（前端 `camel_case` 后查 i18n）。

    判据完全取自 `core.model.coupling_signal` 的因子，本层不重复阈值——
    否则"面板说她饿了"和"心情真的掉得快"就有两个来源，早晚漂移。
    四个码：`mood_from_satiety` / `mood_from_satiety_severe` / `health_from_energy`
    / `health_from_energy_severe`；没有耦合就是空列表（面板显示"目前没有跨轴拖累"）。
    """
    if coupling is None:
        return []
    codes: list[str] = []
    try:
        mood_factor = float(getattr(coupling, "mood_factor", 1.0))
        health_factor = float(getattr(coupling, "health_factor", 1.0))
    except (TypeError, ValueError):
        return []
    if mood_factor >= _MOOD_FACTOR_SEVERE - _FACTOR_EPS:
        codes.append("mood_from_satiety_severe")
    elif mood_factor >= _MOOD_FACTOR_MILD - _FACTOR_EPS:
        codes.append("mood_from_satiety")
    if health_factor <= _HEALTH_FACTOR_SEVERE + _FACTOR_EPS:
        codes.append("health_from_energy_severe")
    elif health_factor <= _HEALTH_FACTOR_MILD + _FACTOR_EPS:
        codes.append("health_from_energy")
    return codes


def _body_rows(axes: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """身体段：按 `STAT_NAMES` 顺序逐轴出明细（值/档/距下一档/今日变化）。

    这里**不重算**任何档位线——原料就是 `core.model.axis_details` 的输出，原样挑字段转发。
    `delta_today=None`（今日无注入锚点）与 `next_tier=None`（已封顶）都保持 `None`，
    由面板整行不渲染。
    """
    rows: list[dict[str, Any]] = []
    for stat in STAT_NAMES:
        raw = axes.get(stat)
        axis = _tier_axis(axes, stat)
        if raw is None or axis is None:
            continue
        tier, _index, _value = axis
        rows.append(
            {
                "stat": stat,
                "tier": tier,
                "tier_index": int(raw.get("tier_index")),
                "value": round(float(raw.get("value")), 1),
                "next_tier": raw.get("next_tier") if isinstance(raw.get("next_tier"), str) else None,
                "to_next": _opt_float(raw.get("to_next")),
                "delta_today": _opt_float(raw.get("delta_today")),
            }
        )
    return rows


def _opt_float(raw: Any) -> float | None:
    """可选的一位小数读数：脏值/缺失都回 `None`，不回 0.0。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _us_view(
    *,
    day_number: Any,
    streak_days: Any,
    gap_hours: Any,
    anniversary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """我们段：相处天数 / 连续陪伴 / 距上次互动 / 纪念日。

    只转发事实，不做文案。`gap_hours=None` = 台账里根本没有"上次互动"（新角色），
    面板据此不渲染那一行；`0.0` 是"刚刚才说过话"，两者绝不混同。
    """
    anniversary_kind: str | None = None
    if isinstance(anniversary, Mapping):
        kind = anniversary.get("kind")
        if isinstance(kind, str) and kind:
            anniversary_kind = kind
    return {
        "day_number": _opt_int(day_number),
        "streak_days": _opt_int(streak_days),
        "gap_hours": _opt_float(gap_hours),
        "anniversary_kind": anniversary_kind,
    }


def _opt_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _mind_view(judgment: Mapping[str, Any] | None) -> dict[str, Any]:
    """她的想法段：把"她自己判断这轮聊得怎么样"摊给主人看（v0.8.0 用户确认要做）。

    只取 `judgment_history` 的**最后一条**（她的最近一次评价）+ 今天说过几次。
    `label` 必须是 `JUDGMENT_LABELS` 里的已登记键才转发——面板拼
    `panel.judgment.<label>`，上了标签却没上文案会显示成空白徽章（那比不显示更糟）。
    历史条目本身不可信（旧分片 / 手动改过 store 的兜底），所以非 Mapping 直接跳过。
    `applied` 原样带出（可正可负），
    因为"她的高兴值多少分"是这一页少数几个诚实的数字之一。
    """
    if not isinstance(judgment, Mapping):
        return {"has_judgment": False, "label": None, "applied": None, "count_today": None, "at": None}
    history = judgment.get("history")
    last: Mapping[str, Any] | None = None
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        for item in history:
            if isinstance(item, Mapping):
                last = item
    label = last.get("label") if isinstance(last, Mapping) else None
    if not isinstance(label, str) or label not in JUDGMENT_LABELS:
        label = None
    return {
        "has_judgment": label is not None,
        "label": label,
        "applied": _opt_float(last.get("applied")) if isinstance(last, Mapping) else None,
        "count_today": _opt_int(judgment.get("count_today")),
        "at": _opt_float(last.get("at")) if isinstance(last, Mapping) else None,
    }


def _today_view(*, meals_today: Any, checked_today: Any, job: Mapping[str, Any] | None, games: Mapping[str, Any] | None, sodas: Any, spoke_today: Any) -> dict[str, Any]:
    """她的一天段：吃了几餐 / 签没签 / 在不在上班 / 玩了没 / 今天开口几次。

    一律"有就报、没有就 `None`"，不把未知折成 0。班次与小游戏只带**呈现字段**
    （id / 剩余秒 / 是否在手），小游戏真值（题答、牌序）从不进这一层。
    """
    job_id: str | None = None
    job_remaining: float | None = None
    if isinstance(job, Mapping):
        raw_job = job.get("id")
        if isinstance(raw_job, str) and raw_job:
            job_id = raw_job
            job_remaining = _opt_float(job.get("remaining_sec"))
    game_active: bool = False
    if isinstance(games, Mapping):
        game_active = isinstance(games.get("active"), Mapping)
    return {
        "meals_today": _opt_int(meals_today),
        "checked_today": bool(checked_today),
        "job_id": job_id,
        "job_remaining_sec": job_remaining,
        "game_active": game_active,
        "sodas": _opt_int(sodas),
        "spoke_today": _opt_int(spoke_today),
    }


def build_state_note(
    *,
    axes: Mapping[str, Mapping[str, Any]],
    rhythm: "DailyRhythm | None" = None,
    coupling: "CouplingSignal | None" = None,
    crisis: bool = False,
    crisis_axes: Sequence[str] = (),
    day_number: Any = None,
    streak_days: Any = None,
    gap_hours: Any = None,
    anniversary: Mapping[str, Any] | None = None,
    judgment: Mapping[str, Any] | None = None,
    meals_today: Any = None,
    checked_today: Any = False,
    job: Mapping[str, Any] | None = None,
    games: Mapping[str, Any] | None = None,
    sodas: Any = None,
    spoke_today: Any = None,
) -> dict[str, Any]:
    """「她此刻的状态」页的数据面：自述键 + 四段档案，全是判据与 ASCII，零文案。

    调用方（`OurLifePlugin.dashboard_context`）负责把已经算好的原料递进来：
    `axes` 来自 `core.model.axis_details`、`rhythm` 来自 `resolve_rhythm`、
    `coupling` 来自 `coupling_signal`、`crisis/crisis_axes` 来自 `core.model` 的同名判据。
    本函数**不再读时钟、不碰 store**，所以同一份输入永远得到同一份输出（可写纯单测）。

    输出键：`voice`（整句键数组，1~3 项）、`voice_count`、`body`、`coupling`（ASCII 码）、
    `rhythm`、`us`、`mind`、`today`。任何一段原料不齐时该段为空/`None` 字段，而不是报错——
    面板是只读视图，不该因为一处缺数据就整页打不开。
    """
    voice = voice_keys(axes, crisis=crisis, crisis_axes=crisis_axes)
    rhythm_view: dict[str, Any] = {
        "phase": None,
        "sleeping": None,
        "hour": None,
        "minute": None,
        "hours_to_sleep": None,
        "hours_to_wake": None,
    }
    if rhythm is not None:
        phase = getattr(rhythm, "phase", None)
        rhythm_view = {
            "phase": phase if isinstance(phase, str) and phase else None,
            "sleeping": bool(getattr(rhythm, "sleeping", False)),
            "hour": _opt_int(getattr(rhythm, "hour", None)),
            "minute": _opt_int(getattr(rhythm, "minute", None)),
            "hours_to_sleep": _opt_float(getattr(rhythm, "hours_to_sleep", None)),
            "hours_to_wake": _opt_float(getattr(rhythm, "hours_to_wake", None)),
        }
    return {
        "voice": voice,
        "voice_count": len(voice),
        "voice_reason": "crisis" if crisis else ("low" if len(voice) == _VOICE_COUNT_LOW else "steady"),
        "body": _body_rows(axes),
        "coupling": coupling_codes(coupling),
        "rhythm": rhythm_view,
        "us": _us_view(
            day_number=day_number,
            streak_days=streak_days,
            gap_hours=gap_hours,
            anniversary=anniversary,
        ),
        "mind": _mind_view(judgment),
        "today": _today_view(
            meals_today=meals_today,
            checked_today=checked_today,
            job=job,
            games=games,
            sodas=sodas,
            spoke_today=spoke_today,
        ),
    }
