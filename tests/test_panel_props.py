"""Hosted TSX 面板的"静默坏 props"门（常驻）。

**为什么需要它**：Hosted UI Kit 的这些属性写错时**运行时不报错**，只是被静默丢弃、
布局默默坏掉（README 坑位 §7 记录过，`plugin/sdk/hosted-ui/index.d.ts` 逐条核对过）。
本机沙箱跑不了 `check-hosted-tsx` 与真实 `tsc`（那两门要求被检文件在宿主仓内），
而**这类错误恰好是"本地全绿、用户看到烂面板"的那一类**——所以在这里补一道文本级门。

门覆盖 `ui/**`（骨架 + shared + components，v0.6.0 拆分后门只看主文件=给拆分留盲区：
hosted-tsx 检查器是顺依赖发现的文件级扫描）真正 import 了的组件，并且刻意排除合法使用同名 prop 的那些
（`Field` 与 `Switch` 的 `label` 是对的）。它不是 `tsc` 的替代品，而是"已知静默坑"的回归网：
宿主升级把某个 prop 改名时，这条门会红，提醒去改面板而不是等用户反馈布局坏了。

`ui/**` 里的**注释**也在扫描范围内（全部文件拼接后扫描）——这是刻意的：注释里写坏 props 会误导后来者，
而且检查器本身也是文本级的（见 README 坑位 §8）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ui"
PANEL = UI_ROOT / "panel.tsx"

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


def _ui_files() -> list[Path]:
    """扫描面 = 整个 `ui/**`（骨架 → shared → components）：
    hosted-tsx 检查器是顺依赖发现的**文件级**扫描（v0.6.0 拆分后门只看主文件=留盲区）。
    固定顺序让门失败信息可复现。"""
    files = [PANEL, UI_ROOT / "shared.tsx", *sorted((UI_ROOT / "components").glob("*.tsx"))]
    return [file for file in files if file.is_file()]


def _panel_source() -> str:
    return "\n".join(file.read_text(encoding="utf-8") for file in _ui_files())


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

    轮询的三条性能闸门各有其形：单飞合并（busy + trailing rerun ref）、后台暂停与
    回可见补拉（visibilitychange）、固定节奏（setInterval + AUTO_REFRESH_MS）。
    v0.4.4 起这条通道同时服务动作后的即时刷新（kit 只对 ActionButton/ActionForm
    兑现 refresh_context，普通 Button 面板必须自己拉——金币/背包"后台扣了前台不动"的根因）。
    """
    source = _panel_source()
    assert "setInterval" in source and "AUTO_REFRESH_MS" in source, (
        "panel must poll the context automatically"
    )
    assert "refreshBusy" in source and "refreshRerun" in source, (
        "refresh must be single-flight with a coalesced trailing rerun, not a request queue"
    )
    assert "visibilitychange" in source, "polling must pause in background / catch up on return"
    assert "<ActionButton" not in source, "manual refresh button retired in v0.4.3"
    assert "actionOf" not in source, "dead helper of the retired button must not come back"


def test_panel_overrides_the_kit_disabled_button_cursor() -> None:
    """v0.4.5 门：kit 的 `.neko-button:disabled` 是 `cursor: wait`（转圈等待光标），

    而我们的禁用只意味着"总开关关了、按不了"，不是"处理中"。面板必须自带
    `not-allowed` 覆盖（经 `<style>` 注入，与宿主样式同特异度、后置获胜）。
    """
    source = _panel_source()
    assert "<style>{PANEL_STYLE_OVERRIDES}</style>" in source, (
        "panel must inject its style overrides"
    )
    assert ".neko-button:disabled { cursor: not-allowed; }" in source, (
        "disabled buttons must show not-allowed, not the kit's wait spinner"
    )


def test_panel_refreshes_context_after_every_successful_action() -> None:
    """v0.4.4 门：所有动作走同一个 run()，成功路径必须拉一次 context。

    断言全文件只有 refreshContext 一处 `props.api.refresh()`：动作处理器与轮询
    都只能通过这条单飞通道刷新——谁再手写第二处，就是绕过了合并闸门。
    """
    source = _panel_source()
    assert "if (result) await refreshContext()" in source, (
        "run() must refresh the context on every successful action"
    )
    assert source.count("props.api.refresh()") == 1, (
        "props.api.refresh() must live only inside the shared refreshContext channel"
    )


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


def test_panel_is_split_into_shared_and_components() -> None:
    """v0.6.0 拆分门：骨架只留骨架，可视模块各归各家，工具函数全仓只有一份。

    拆分的理由（"空空的"病灶）在 CHANGELOG 第十四轮之后：区块焊在 900 行的单文件里
    不等于信息密度——想细化一个区块先要敢改它。拆开后谁想把某个块搬回骨架
    或者再造一份 sparkline/tierTone，这条门会问清楚。
    """
    skeleton = PANEL.read_text(encoding="utf-8")
    for module in (
        "./shared",
        "./components/axis_cards",
        "./components/day_band",
        "./components/rhythm_bar",
        "./components/bag",
        "./components/timeline",
    ):
        assert f'from "{module}"' in skeleton, f"panel skeleton must import {module}"
    # 工具函数只许住在 shared.tsx（对偶性纪律：两处需要的逻辑只写一遍）。
    assert "function sparkline" not in skeleton, "helpers must live in ui/shared.tsx"
    shared = (UI_ROOT / "shared.tsx").read_text(encoding="utf-8")
    assert "function sparkline" in shared and "function hourBars" in shared


def test_overview_tab_carries_the_detailed_blocks() -> None:
    """v0.6.0 总览页门：今日带 + 五轴卡 + 作息条必须在；旧"孤立 KeyValue 墙"不回潮。

    五轴卡的明细文案（距升档/今日变化）由 axis_cards 渲染；作息条的字符柱
    依赖 hourBars 归一——这两处是本轮"把空的地方填上"的主体，拆掉任何一处
    都等于退回 v0.5.x 的稀疏总览。
    """
    skeleton = PANEL.read_text(encoding="utf-8")
    for element in ("<DayBand", "<AxisCards", "<RhythmBar", "<Timeline", "<Bag "):
        assert element in skeleton, f"panel must mount {element}"
    axis_cards = (UI_ROOT / "components" / "axis_cards.tsx").read_text(encoding="utf-8")
    for key in ("panel.axis.toNext", "panel.axis.maxTier", "panel.axis.delta"):
        assert f'"{key}"' in axis_cards, f"axis cards lost detail line {key}"
    rhythm = (UI_ROOT / "components" / "rhythm_bar.tsx").read_text(encoding="utf-8")
    assert "hourBars" in rhythm and "our-life-hour-sleep" in rhythm, (
        "rhythm card must keep the 24-cell day bar with sleep shading"
    )


def test_axes_detail_labels_come_from_context_not_local_math() -> None:
    """面板不做档位运算：距升档/今日变化只读 context 的 axes，不得在 TSX 里碰档位线。

    档位线的唯一来源是 Python 的 `TIER_BOUNDS`（`core.model.axis_details` 现算）；
    如果哪天有人在 TSX 里写 20/40/60/80 手算"还差多少分"，这条门红——那正是
    "面板说还差 3 分、下一拍却升档了"的错位起源。
    """
    for file in _ui_files():
        source = file.read_text(encoding="utf-8")
        assert not re.search(r"\b(20|40|60|80)\.0\b", source), (
            f"{file.name} hard-codes tier bounds; read them from context.axes"
        )
