"""文档 ↔ 配置同源门（常驻）。

forever_companion 台账里反复出现的债就是"默认值漂移"：README 改了、plugin.toml 没改、
或者配置示例与代码默认值各说各话。这里把它变成会红的门：

- `plugin.toml` 与 `config.example.toml` 的 `[our_life]` 族必须**键相同、值相同**；
- 这两份 TOML 的默认值必须与 `core/configuration.py` 的 dataclass 默认值**逐一相等**；
- `config.example.toml` 不得出现任何 `[plugin]` 表（校验器会报 error 的同一个约束）；
- `[plugin.store].enabled` 必须为 true（否则 PluginStore 会静默不落盘）。
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any, Mapping

from our_life.core.configuration import (
    DecaySettings,
    EconomySettings,
    EventSettings,
    FeedbackSettings,
    GrowthSettings,
    InjectSettings,
    NeglectSettings,
    OurLifeSettings,
    RhythmSettings,
)

ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = "our_life"
SUB_SECTIONS = (
    ("decay", DecaySettings),
    ("rhythm", RhythmSettings),
    ("economy", EconomySettings),
    ("growth", GrowthSettings),
    ("feedback", FeedbackSettings),
    ("events", EventSettings),
    ("neglect", NeglectSettings),
    ("inject", InjectSettings),
)


def _load(name: str) -> Mapping[str, Any]:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def _flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _family(flattened: Mapping[str, Any]) -> dict[str, Any]:
    return {
        path: value
        for path, value in flattened.items()
        if path == FAMILY_ROOT or path.startswith(f"{FAMILY_ROOT}.")
    }


def _normalize(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [round(float(item), 6) if isinstance(item, (int, float)) else item for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    return value


def _code_defaults() -> dict[str, Any]:
    root = OurLifeSettings()
    out: dict[str, Any] = {
        f"{FAMILY_ROOT}.enabled": root.enabled,
        f"{FAMILY_ROOT}.tick_seconds": root.tick_seconds,
    }
    for name, cls in SUB_SECTIONS:
        instance = cls()
        for field in dataclasses.fields(cls):
            out[f"{FAMILY_ROOT}.{name}.{field.name}"] = getattr(instance, field.name)
    return out


# ---------------------------------------------------------------------------


def test_plugin_toml_and_example_agree_on_the_business_family() -> None:
    manifest = _family(_flatten(_load("plugin.toml")))
    example = _family(_flatten(_load("config.example.toml")))
    assert manifest, "plugin.toml must declare the [our_life] family"
    assert set(manifest) == set(example), (
        "plugin.toml and config.example.toml disagree on [our_life] keys: "
        f"only_manifest={sorted(set(manifest) - set(example))} "
        f"only_example={sorted(set(example) - set(manifest))}"
    )
    for path in sorted(manifest):
        assert _normalize(manifest[path]) == _normalize(example[path]), (
            f"default drift for {path}: plugin.toml={manifest[path]!r} example={example[path]!r}"
        )


def test_toml_defaults_match_code_defaults() -> None:
    manifest = _family(_flatten(_load("plugin.toml")))
    defaults = _code_defaults()
    assert set(manifest) == set(defaults), (
        "plugin.toml and core/configuration.py disagree on [our_life] keys: "
        f"only_toml={sorted(set(manifest) - set(defaults))} "
        f"only_code={sorted(set(defaults) - set(manifest))}"
    )
    for path in sorted(defaults):
        assert _normalize(manifest[path]) == _normalize(defaults[path]), (
            f"code default differs for {path}: toml={manifest[path]!r} code={defaults[path]!r}"
        )


def test_config_example_has_no_plugin_tables() -> None:
    example = _load("config.example.toml")
    forbidden = [key for key in example if key == "plugin" or key.startswith("plugin.")]
    assert not forbidden, f"config.example.toml must not contain [plugin*] tables: {forbidden}"


def test_store_is_enabled() -> None:
    manifest = _load("plugin.toml")
    assert manifest["plugin"]["store"]["enabled"] is True, (
        "PluginStore silently no-ops when disabled, so it must stay enabled"
    )


def test_identity_fields_are_consistent_with_the_directory() -> None:
    manifest = _load("plugin.toml")
    plugin = manifest["plugin"]
    assert plugin["id"] == "our_life"
    assert plugin["type"] == "plugin"
    assert plugin["entry"] == "plugin.plugins.our_life:OurLifePlugin"
    assert plugin["version"] == _load("pyproject.toml")["project"]["version"]


def test_panel_declares_the_context_it_exposes() -> None:
    manifest = _load("plugin.toml")
    panels = manifest["plugin"]["ui"]["panel"]
    assert len(panels) == 1
    panel = panels[0]
    assert panel["context"] == "dashboard"
    assert panel["entry"] == "ui/panel.tsx"
    assert panel["permissions"] == ["state:read", "config:read", "action:call"]


# ---------------------------------------------------------------------------
# 跨层一致性：配置里指的物品必须真的存在、且真的能当饭吃
# ---------------------------------------------------------------------------


def test_staple_item_points_at_a_real_food_item() -> None:
    """`staple_item_id` 配错会让自动进食**静默**退化（`meal_plan` 找不到它就换别的吃，
    一件都没有时才饿着），所以把它钉在门上：必须存在、必须是食物。
    """
    from our_life.core.economy import item, meal_plan

    economy = OurLifeSettings().economy
    staple = item(economy.staple_item_id)
    assert staple is not None, f"staple_item_id={economy.staple_item_id!r} is not in the catalog"
    assert staple.food, f"{staple.id!r} is not edible, so she could never eat it"
    # 顺带确认它真的会被 meal_plan 选中（不是"存在于表里但用不上"）
    from our_life.core.economy import Inventory

    plan = meal_plan(
        Inventory.from_mapping({staple.id: 2}),
        staple_item_id=staple.id,
        satiety=10.0,
        threshold=economy.meal_threshold,
    )
    assert plan == staple.id


def test_meal_threshold_sits_inside_the_satisfied_band() -> None:
    """"她饿了"与面板的饱食档位必须是同一个信号（见 `core/economy` 模块 docstring）。

    阈值落在 satisfied 档（下界 40）之内，面板上读作"饱食掉到『吃饱了』以下"。
    """
    from our_life.core.model import TIER_BOUNDS

    threshold = OurLifeSettings().economy.meal_threshold
    assert TIER_BOUNDS[2] <= threshold < TIER_BOUNDS[3], (
        f"meal_threshold={threshold} is outside the satisfied tier band"
    )


# ---------------------------------------------------------------------------
# 跨层一致性：反馈闭环的两条不变式（第 ①③ 条闸门的配置面）
# ---------------------------------------------------------------------------


def test_feedback_correction_can_never_outweigh_a_real_turn() -> None:
    """闸门 ① 的不变式：**她报一次开心的分量必须小于主人多说一句话的分量**。

    这是整个反馈闭环的安全底线——工具再被滥用也压不过真实互动。
    它由 `core/judgment._TURN_GAIN_SCALE < 1` 保证，而这里把它钉在门上：
    单次判断的最大修正量（权重 1.0 的标签、会话首次、不计递减）必须 < `turn_mood_gain`。
    """
    from our_life.core.judgment import JUDGMENT_WEIGHTS, judge

    settings = OurLifeSettings()
    # 会话首次（session_turns=0）时递减系数为 1，所以这就是单次最大幅度
    result = judge(
        label="wonderful",
        strength=1.0,
        feedback=settings.feedback,
        growth=settings.growth,
        enabled=True,
        last_touch_at=1000.0,
        last_judgment_at=None,
        session_turns=0,
        day_used_add=0.0,
        day_used_subtract=0.0,
    )
    assert result.applied
    assert abs(result.mood) < settings.growth.turn_mood_gain, (
        "a single judgment must stay strictly below one real turn's growth "
        f"(judgment={result.mood}, turn={settings.growth.turn_mood_gain})"
    )
    assert abs(result.health) < settings.growth.turn_health_gain
    assert abs(result.affection) < settings.growth.turn_affection_gain
    # 权重表本身也要能自证：最大正向标签的权重是 1.0，其余都不超过它
    assert max(JUDGMENT_WEIGHTS.values()) == 1.0


def test_feedback_negative_budget_is_not_larger_than_the_positive_one() -> None:
    """闸门 ③ 的不变式：**负向额度不许超过正向**。

    让工具能扣分等于给模型一条惩罚通道，所以负向天花板刻意只有正向的一半。
    谁把 `daily_subtract_points` 调过头，这条门会红——那是有意的。
    """
    feedback = OurLifeSettings().feedback
    assert feedback.daily_add_points > 0.0, "正向预算为 0 等于反馈闭环形同关闭"
    assert 0.0 <= feedback.daily_subtract_points <= feedback.daily_add_points, (
        "the negative budget must not rival the positive one: "
        f"add={feedback.daily_add_points} subtract={feedback.daily_subtract_points}"
    )
