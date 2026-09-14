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
    GrowthSettings,
    InjectSettings,
    NeglectSettings,
    OurLifeSettings,
)

ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = "our_life"
SUB_SECTIONS = (
    ("decay", DecaySettings),
    ("growth", GrowthSettings),
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
