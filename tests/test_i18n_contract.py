"""i18n 契约门（常驻）。

沿用 forever_companion 第九轮定下的形态，按本插件的中英双语规模裁剪：

1. **码形门**：面板可达码必须全部匹配 `^[a-z][a-z0-9_]*$`。
2. **码↔键同步门**：每个码都要有 `panel.errors.<camelCase>` 键（反向也要：不能有孤立键）。
3. **成功档同步门**：入口返回的 note 码也要有 `panel.msg.<camelCase>` 键。
4. **键集一致门**：两种语言的键集必须完全相同。
5. **引用面门**：`ui/**`（v0.6.0 拆分后含骨架与组件）里 `t("字面量")` 与 `__init__.py` 里 `tr("字面量")` 引用的键都必须存在。
6. **结构门**：动态拼接族（tier / trigger）必须逐项存在——拼接键不在引用面门的覆盖面内。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from our_life.core.codes import PANEL_ERROR_CODES, camel_case, is_panel_code
from our_life.core.events import EVENT_KEYS
from our_life.core.injection import (
    TRIGGER_ANNIVERSARY,
    TRIGGER_COMPANY,
    TRIGGER_CRISIS,
    TRIGGER_DAILY_GREET,
    TRIGGER_HUNGRY,
    TRIGGER_INTERVAL,
    TRIGGER_JOB,
    TRIGGER_JUDGMENT,
    TRIGGER_STAGED_EVENT,
    TRIGGER_TIER_CHANGE,
    TRIGGER_TIRED,
)
from our_life.core.jobs import JOB_IDS
from our_life.core.model import (
    AFFECTION_TIERS,
    ENERGY_TIERS,
    HEALTH_TIERS,
    MOOD_TIERS,
    SATIETY_TIERS,
    STAT_NAMES,
)

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("zh-CN", "en")

# 入口在成功路径返回的 note 码 → 面板 `panel.msg.<camel>`  toast 文案
SUCCESS_NOTES = (
    "stats_loaded",
    "stat_updated",
    "stats_reset",
    "enabled",
    "disabled",
    "shop_purchased",
    "care_applied",
    "coin_updated",
    "checkin_done",
    "job_started",
    "job_returned",
    "makeup_done",
    "focus_set",
    "focus_cleared",
)

RE_TR_LITERAL = re.compile(r'\btr\(\s*"([^"]+)"')
RE_T_LITERAL = re.compile(r'\bt\(\s*"([^"]+)"')


def _load(locale: str) -> dict[str, str]:
    raw = json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"i18n/{locale}.json must be a flat object"
    return raw


def _flatten_keys(source: str, pattern: re.Pattern[str]) -> set[str]:
    return set(pattern.findall(source))


# ---------------------------------------------------------------------------
# 1. 码形门
# ---------------------------------------------------------------------------


def test_panel_codes_are_stable_ascii() -> None:
    assert PANEL_ERROR_CODES, "there must be at least one panel code"
    for code in PANEL_ERROR_CODES:
        assert is_panel_code(code), f"panel code is not a stable ascii code: {code!r}"


def test_camel_case_matches_the_frontend_convention() -> None:
    assert camel_case("not_enabled") == "notEnabled"
    assert camel_case("stats_reset") == "statsReset"
    assert camel_case("enabled") == "enabled"


# ---------------------------------------------------------------------------
# 2/3. 码 ↔ 键同步门（双向）
# ---------------------------------------------------------------------------


def test_every_panel_code_has_both_locale_keys() -> None:
    for locale in LOCALES:
        messages = _load(locale)
        for code in sorted(PANEL_ERROR_CODES):
            key = f"panel.errors.{camel_case(code)}"
            assert key in messages, f"{locale} is missing {key}"


def test_no_orphan_error_keys() -> None:
    expected = {f"panel.errors.{camel_case(code)}" for code in PANEL_ERROR_CODES}
    for locale in LOCALES:
        messages = _load(locale)
        orphans = {key for key in messages if key.startswith("panel.errors.")} - expected
        assert not orphans, f"{locale} has error keys without a code: {sorted(orphans)}"


def test_success_notes_have_toast_keys() -> None:
    for locale in LOCALES:
        messages = _load(locale)
        for note in SUCCESS_NOTES:
            key = f"panel.msg.{camel_case(note)}"
            assert key in messages, f"{locale} is missing {key}"


# ---------------------------------------------------------------------------
# 4. 键集一致门
# ---------------------------------------------------------------------------


def test_locales_have_identical_key_sets() -> None:
    reference = set(_load(LOCALES[0]))
    for locale in LOCALES[1:]:
        keys = set(_load(locale))
        assert keys == reference, (
            f"{locale} key set differs from {LOCALES[0]}: "
            f"missing={sorted(reference - keys)} extra={sorted(keys - reference)}"
        )


def test_locale_values_are_non_empty() -> None:
    for locale in LOCALES:
        for key, value in _load(locale).items():
            assert isinstance(value, str) and value.strip(), f"{locale}:{key} is empty"


# ---------------------------------------------------------------------------
# 5. 引用面门
# ---------------------------------------------------------------------------


def test_tsx_referenced_keys_exist() -> None:
    # v0.6.0 拆分：引用面扩到整个 ui/**（骨架 + shared + components）。
    source = "\n".join(
        file.read_text(encoding="utf-8") for file in sorted((ROOT / "ui").rglob("*.tsx"))
    )
    keys = _flatten_keys(source, RE_T_LITERAL)
    assert keys, "expected the panel to reference i18n literals"
    for locale in LOCALES:
        messages = _load(locale)
        for key in sorted(keys):
            assert key in messages, f"{locale} is missing tsx key {key}"


def test_python_tr_referenced_keys_exist() -> None:
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    keys = _flatten_keys(source, RE_TR_LITERAL)
    assert keys, "expected the plugin to declare i18n literals via tr()"
    for locale in LOCALES:
        messages = _load(locale)
        for key in sorted(keys):
            assert key in messages, f"{locale} is missing python key {key}"


# ---------------------------------------------------------------------------
# 6. 结构门（动态拼接族）
# ---------------------------------------------------------------------------


def test_tier_keys_exist_for_every_stat_and_tier() -> None:
    families = {
        "affection": AFFECTION_TIERS,
        "mood": MOOD_TIERS,
        "health": HEALTH_TIERS,
        "satiety": SATIETY_TIERS,
        "energy": ENERGY_TIERS,
    }
    assert set(families) == set(STAT_NAMES)
    for locale in LOCALES:
        messages = _load(locale)
        for stat, tiers in families.items():
            for tier in tiers:
                key = f"panel.tier.{stat}.{tier}"
                assert key in messages, f"{locale} is missing {key}"


def test_trigger_keys_exist_for_every_trigger() -> None:
    triggers = (
        TRIGGER_TIER_CHANGE,
        TRIGGER_CRISIS,
        TRIGGER_DAILY_GREET,
        TRIGGER_INTERVAL,
        TRIGGER_COMPANY,
        TRIGGER_HUNGRY,
        TRIGGER_TIRED,
        TRIGGER_ANNIVERSARY,
        TRIGGER_JUDGMENT,
        TRIGGER_STAGED_EVENT,
        TRIGGER_JOB,
    )
    for locale in LOCALES:
        messages = _load(locale)
        for trigger in triggers:
            key = f"panel.trigger.{trigger}"
            assert key in messages, f"{locale} is missing {key}"


def test_job_names_exist_for_every_job() -> None:
    """工作名必须两语齐全（v0.7.0 打工）。

    面板按 `panel.job.<id>` 动态拼键渲染目录，拼接键不在引用面门覆盖面内——
    少一个键只会显示成空白。与事件族同一理由，逐项钉住。
    """
    assert JOB_IDS, "there must be at least one job"
    for locale in LOCALES:
        messages = _load(locale)
        for job_id in JOB_IDS:
            key = f"panel.job.{job_id}"
            assert key in messages, f"{locale} is missing {key}"
            assert messages[key].strip(), f"{locale}:{key} is empty"


def test_event_keys_exist_for_every_staged_event() -> None:
    """阶段性事件的名字必须两语齐全（v0.4.0）。

    与 tier / trigger 两个族同一个理由：面板按 `panel.event.<key>` **动态拼键**
    渲染"她经历过什么"，而拼接键不在引用面门（`t("字面量")` 正则）的覆盖面内——
    少一个键不会让任何门变红，只会在面板上显示成空白。所以在这里逐项钉住。
    """
    assert EVENT_KEYS, "there must be at least one staged event"
    for locale in LOCALES:
        messages = _load(locale)
        for event in EVENT_KEYS:
            key = f"panel.event.{event}"
            assert key in messages, f"{locale} is missing {key}"
            assert messages[key].strip(), f"{locale}:{key} is empty"
