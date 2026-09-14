"""Hosted TSX 面板的"静默坏 props"门（常驻）。

**为什么需要它**：Hosted UI Kit 的这些属性写错时**运行时不报错**，只是被静默丢弃、
布局默默坏掉（README 坑位 §7 记录过，`plugin/sdk/hosted-ui/index.d.ts` 逐条核对过）。
本机沙箱跑不了 `check-hosted-tsx` 与真实 `tsc`（那两门要求被检文件在宿主仓内），
而**这类错误恰好是"本地全绿、用户看到烂面板"的那一类**——所以在这里补一道文本级门。

门只覆盖 `ui/panel.tsx` 真正 import 了的组件，并且刻意排除合法使用同名 prop 的那些
（`Field` 与 `Switch` 的 `label` 是对的）。它不是 `tsc` 的替代品，而是"已知静默坑"的回归网：
宿主升级把某个 prop 改名时，这条门会红，提醒去改面板而不是等用户反馈布局坏了。

`ui/panel.tsx` 里的**注释**也在扫描范围内——这是刻意的：注释里写坏 props 会误导后来者，
而且检查器本身也是文本级的（见 README 坑位 §8）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ui" / "panel.tsx"

# 组件 → 它**没有**的 prop（写在面板里会被静默丢弃）
FORBIDDEN_PROPS: dict[str, tuple[str, ...]] = {
    "Grid": ("columns",),
    "StatCard": ("text", "tone"),
    "KeyValue": ("data",),  # 面板一律走 items（data 也支持，但混用会出现"改了不生效"的错觉）
    "Alert": ("title",),  # Alert 用 message
    "EmptyState": ("message",),  # EmptyState 用 description
    "Switch": ("checkedLabel",),
    "NumberInput": ("label",),  # NumberInput 没有 label，要包 Field
    "Select": ("label",),  # Select 没有 label，要包 Field
    "Progress": ("percent",),  # Progress 用 value
    "Slider": ("label",),  # Slider 没有 label（只有 showValue），要包 Field
    "SegmentedControl": ("label",),  # 同理：没有 label，标签文字另放 Text
}

# 合法使用同名 prop 的组件：不做检查（`Field`/`Switch` 的 label 就是对的）
EXEMPT_COMPONENTS = frozenset({"Field", "Switch", "Checkbox", "Accordion"})

IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*[\"']@neko/plugin-ui[\"']", re.S)


def _panel_source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _imported_components(source: str) -> set[str]:
    names: set[str] = set()
    for block in IMPORT_RE.findall(source):
        for raw in block.split(","):
            name = raw.strip().split(" as ")[0].strip()
            if name and name[0].isupper():
                names.add(name)
    return names


def _component_blocks(source: str) -> dict[str, str]:
    """把每个 `<Component ...>` 开标签的文本抓出来（`>` 之前的属性区）。"""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"<([A-Z][A-Za-z0-9]*)([^>]*)>", source, re.S):
        name, attrs = match.group(1), match.group(2)
        blocks[f"{name}@{match.start()}"] = attrs
    return blocks


def test_panel_only_uses_props_the_kit_actually_has() -> None:
    source = _panel_source()
    imported = _imported_components(source)
    assert imported, "expected the panel to import components from @neko/plugin-ui"

    offenders: list[str] = []
    for label, attrs in _component_blocks(source).items():
        name = label.split("@", 1)[0]
        if name in EXEMPT_COMPONENTS or name not in imported:
            continue
        for prop in FORBIDDEN_PROPS.get(name, ()):
            # 属性名的形态：`prop=` 或 `prop:` （JSX 属性或对象字面量两种写法都抓）
            if re.search(rf"(^|[\s{{,]){re.escape(prop)}([\s=:])", attrs):
                offenders.append(f"<{name}> uses {prop!r} (silently dropped by the kit)")
    assert not offenders, "panel uses props the Hosted UI Kit does not have: " + "; ".join(offenders)


def test_panel_does_not_use_the_global_facade_bare() -> None:
    """hosted-tsx 检查器是**文本级**规则（连注释都不跳过）：裸标识符形态会被拒收。

    这里复刻同一条判据（`(^|[^\\w.])api\\.`），并额外确认面板改用了 `props.api` 成员访问。
    """
    source = _panel_source()
    assert not re.search(r"(^|[^\w.])api\.", source, re.M), (
        "panel must not use the global facade object directly; use props.api"
    )
    assert "props.api.call(" in source, "expected the panel to drive actions through props.api"


def test_panel_uses_tabbed_layout_with_a_persistent_status_band() -> None:
    """v0.4.1 布局门：状态带常驻 + Tabs 四页，防止改版被无意打回单列长滚。"""
    source = _panel_source()
    imported = _imported_components(source)
    assert "Tabs" in imported and "StatusBadge" in imported, "panel must keep the tabbed layout"
    for tab_key in ("panel.tab.overview", "panel.tab.life", "panel.tab.her", "panel.tab.admin"):
        assert f't("{tab_key}")' in source, f"missing tab entry {tab_key}"
    for band_key in ("panel.band.day", "panel.band.streak", "panel.band.crisis"):
        assert f't("{band_key}"' in source, f"missing status band key {band_key}"
    assert '<Tabs id="our_life.main"' in source, "Tabs must keep the persisted id"
    # v0.4.2：窄面板自适应与真机适配面必须在（防止无意改回固定列/输入框）。
    for affordance in ("<Columns", "<Slider", "<SegmentedControl"):
        assert affordance in source, f"panel must keep the v0.4.2 affordance {affordance}"
    assert 'run("focus"' in source, "shard switcher must drive the focus entry"


def test_panel_auto_refreshes_and_has_no_manual_refresh_button() -> None:
    """v0.4.3 刷新门：面板靠轮询自动同步，手动「刷新」按钮不得回潮。

    轮询的三条性能闸门各有其形：防重入（busy ref）、后台暂停与回可见补拉
    （visibilitychange）、固定节奏（setInterval + AUTO_REFRESH_MS）。
    动作后的即时刷新走宿主的 refresh_context=True，不在此门范围。
    """
    source = _panel_source()
    assert "setInterval" in source and "AUTO_REFRESH_MS" in source, (
        "panel must poll the context automatically"
    )
    assert "props.api.refresh()" in source, "auto refresh must go through props.api.refresh"
    assert "refreshBusy" in source, "polling must guard against overlapping refreshes"
    assert "visibilitychange" in source, "polling must pause in background / catch up on return"
    assert "<ActionButton" not in source, "manual refresh button retired in v0.4.3"
    assert "actionOf" not in source, "dead helper of the retired button must not come back"


def test_panel_has_a_single_default_function_export() -> None:
    source = _panel_source()
    # 只数真正的导出语句（注释里也会出现这个词，见本模块 docstring 的说明）
    exports = re.findall(r"^\s*export default\s", source, re.M)
    assert len(exports) == 1, f"expected exactly one default export, found {len(exports)}"
    assert re.search(r"^\s*export default function Panel\(", source, re.M), (
        "hosted TSX must export a default function component"
    )
    assert "import(" not in source, "dynamic import() is not supported in hosted TSX"


def test_panel_only_imports_from_the_kit_or_relative_paths() -> None:
    """裸模块 import（npm 包）在运行时会被拒；只允许 `@neko/plugin-ui` 与相对路径。"""
    source = _panel_source()
    modules = re.findall(r"from\s*[\"']([^\"']+)[\"']", source)
    assert modules, "expected the panel to import the kit"
    for module in modules:
        assert module.startswith(".") or module in {"@neko/plugin-ui", "neko:ui"}, (
            f"hosted TSX may not import {module!r}"
        )
