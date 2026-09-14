"""测试夹具。

沿用 catgirl_seiyuu / forever_companion / your_memory 三仓的测试基建，三件事：

1. **桩掉宿主 SDK**（`plugin.sdk.plugin`）：插件代码只从公共门面导入，桩要足够完整，
   才能把仓根包真的加载起来，让入口、装饰器元数据、角色归属这些行为在没有 N.E.K.O
   宿主的机器上也能被测。真宿主可用时（在仓库内跑）绝不覆盖。
2. **把仓根加载为包 `our_life`**：仓库目录名 `n.e.k.o_plugin_our_life` 不是合法 Python
   标识符，且插件根自带 `__init__.py`（它是包本体，不是测试包标记）。
3. **挡住 pytest 的 Package 节点**：pytest 的 importlib 模式会把仓根建成 `<Package ...>`
   节点，并在 setup 时以模块名 `"__init__"` 导入 `ROOT/__init__.py`——那里有顶层相对导入，
   没有父包就会 "attempted relative import with no known parent package"。
   两道防线：预注册 `sys.modules["__init__"]` 让导入命中缓存；`pytest_collect_directory`
   一律返回 Dir 不建 Package。

另提供 `FakeConfig` / `FakeStore` / `FakeBus` / `build_plugin`：入口级测试用假宿主。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "our_life"


# ---------------------------------------------------------------------------
# 挂载态防护：CI 会把仓库挂到宿主 plugin/plugins/<id>/ 包树里跑测试
# ---------------------------------------------------------------------------


def _pre_register_parent_packages() -> None:
    """沿目录向上为每个含 `__init__.py` 的父包预注册轻量桩。

    挂载态下 pytest 会把外层父包（宿主 `plugin/__init__.py` 带 pydantic 等重型导入）
    一并导入，轻量测试 venv 里没有那些依赖 → 整片收集错误。这里的桩让父包解析
    在 sys.modules 命中即止。独立仓库模式下根目录不在任何包链里，本函数无操作。
    """
    chain: list[tuple[str, Path]] = []
    current = ROOT
    while True:
        if not (current / "__init__.py").is_file():
            break
        chain.append((current.name, current))
        parent = current.parent
        if parent == current:
            break
        current = parent
    if len(chain) < 2:
        return
    chain.reverse()
    dotted_parts: list[str] = []
    for name, directory in chain:
        dotted_parts.append(name)
        dotted = ".".join(dotted_parts)
        if dotted in sys.modules:
            continue
        stub = types.ModuleType(dotted)
        stub.__file__ = str(directory / "__init__.py")
        stub.__path__ = [str(directory)]  # type: ignore[attr-defined]
        sys.modules[dotted] = stub


# ---------------------------------------------------------------------------
# SDK 桩
# ---------------------------------------------------------------------------


def _record_meta(func: Any, dargs: tuple[Any, ...], dkwargs: dict[str, Any]) -> Any:
    meta = getattr(func, "__neko_stub_meta__", None)
    if isinstance(meta, list):
        meta.append({"args": dargs, "kwargs": dkwargs})
    else:
        setattr(func, "__neko_stub_meta__", [{"args": dargs, "kwargs": dkwargs}])
    if dkwargs.get("id"):
        setattr(func, "__neko_stub_id__", dkwargs["id"])
    return func


def _passthrough_decorator(*dargs: Any, **dkwargs: Any):
    """装饰器原样返回函数，并把调用参数记在函数上，供测试断言契约面。"""

    def decorate(func: Any) -> Any:
        return _record_meta(func, dargs, dkwargs)

    return decorate


def _identity_decorator(target: Any) -> Any:
    return target


def _build_facade() -> types.ModuleType:
    facade = types.ModuleType("plugin.sdk.plugin")

    @dataclass(frozen=True)
    class Ok:
        value: Any = None

        def is_ok(self) -> bool:
            return True

    @dataclass(frozen=True)
    class Err:
        error: Any = None

        def is_ok(self) -> bool:
            return False

    class SdkError(RuntimeError):
        def __init__(self, message: str, *, code: str | None = None, details: Any = None):
            super().__init__(message)
            self.code = code
            self.details = details

    def unwrap_or(result: Any, default: Any = None) -> Any:
        return result.value if isinstance(result, Ok) else default

    def tr(key: str, *, default: str = "", **params: Any) -> dict[str, Any]:
        # 与生产签名严格同构（default 是 keyword-only）
        return {"$i18n": key, "default": default, "params": params}

    class NekoPluginBase:
        """最小宿主基类：只提供入口代码真正用到的属性。"""

        def __init__(self, ctx: Any):
            self.ctx = ctx
            self.plugin_id = str(getattr(ctx, "plugin_id", "our_life"))
            self.plugin_dir = ROOT
            self.config_dir = ROOT
            self.storage_dir = ROOT / ".stub-storage"
            self.metadata: dict[str, Any] = {}
            self.logger = getattr(ctx, "logger", logging.getLogger("our_life.stub"))
            self.store = getattr(ctx, "store", None)
            self.config = getattr(ctx, "config", None)
            self.bus = getattr(ctx, "bus", None)

        def push_message(self, **kwargs: Any) -> dict[str, Any]:
            ctx = self.ctx
            pushed = getattr(ctx, "pushed", None)
            if isinstance(pushed, list):
                pushed.append(dict(kwargs))
            return {"submitted": True}

    ui = types.SimpleNamespace(context=_passthrough_decorator, action=_passthrough_decorator)

    for name in (
        "plugin_entry",
        "lifecycle",
        "timer_interval",
        "message",
        "on_event",
        "custom_event",
        "hook",
        "before_entry",
        "after_entry",
        "around_entry",
        "replace_entry",
        "llm_tool",
        "quick_action",
    ):
        setattr(facade, name, _passthrough_decorator)

    @dataclass(frozen=True)
    class PluginMeta:
        id: str = ""
        name: str = ""

    facade.Ok = Ok  # type: ignore[attr-defined]
    facade.Err = Err  # type: ignore[attr-defined]
    facade.SdkError = SdkError  # type: ignore[attr-defined]
    facade.unwrap_or = unwrap_or  # type: ignore[attr-defined]
    facade.tr = tr  # type: ignore[attr-defined]
    facade.NekoPluginBase = NekoPluginBase  # type: ignore[attr-defined]
    facade.neko_plugin = _identity_decorator  # type: ignore[attr-defined]
    facade.PluginMeta = PluginMeta  # type: ignore[attr-defined]
    facade.ui = ui  # type: ignore[attr-defined]
    return facade


def ensure_sdk_stub() -> bool:
    """把 `plugin.sdk.plugin` **无条件**换成桩门面，返回 True。

    为什么无条件：插件自己的测试要同时活在两种形态下，而真 SDK 的 `NekoPluginBase` 会
    自己建一套真通道（真 `PluginStore` / `PluginConfig`），`bus` 更是**只读 property**——
    测试既没法接管，也一碰就 `TransportError: ctx.get_own_config is not available`。
    结果是"独立仓全绿、市场 CI 挂载态全红"。

    测试要验的是**本插件的逻辑**（数值模型、归属判定、开关语义、投递参数），不是宿主 SDK
    的内部实现；所以这里统一用桩，让两种形态行为一致。挂载态真正要验的东西（裸 id 目录、
    entry 路径解析、清单校验、打包）由 `neko-plugin check -r` 自己覆盖，不受这里影响。

    探测用 `importlib.import_module` 而不是裸 `import`：CI 的 ruff 门带 `--ignore-noqa`，
    裸导入上的 `# noqa: F401` 不作数，会被判成"未使用导入"并直接删掉那一行。
    """
    facade = _build_facade()
    existing_plugin = sys.modules.get("plugin")
    existing_sdk = sys.modules.get("plugin.sdk")
    if isinstance(existing_plugin, types.ModuleType):
        plugin_pkg = existing_plugin
    else:
        plugin_pkg = types.ModuleType("plugin")
        plugin_pkg.__path__ = []  # type: ignore[attr-defined]
    if isinstance(existing_sdk, types.ModuleType):
        sdk_pkg = existing_sdk
    else:
        sdk_pkg = types.ModuleType("plugin.sdk")
        sdk_pkg.__path__ = []  # type: ignore[attr-defined]
    plugin_pkg.sdk = sdk_pkg  # type: ignore[attr-defined]
    sdk_pkg.plugin = facade  # type: ignore[attr-defined]
    sys.modules["plugin"] = plugin_pkg
    sys.modules["plugin.sdk"] = sdk_pkg
    sys.modules["plugin.sdk.plugin"] = facade
    return True


# ---------------------------------------------------------------------------
# 仓根 → 包
# ---------------------------------------------------------------------------


def register_plugin_package() -> types.ModuleType:
    """把 ROOT 的 `__init__.py` 加载为包 `our_life`（幂等）。"""
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 显式补 __path__ / __package__：缺了它们，子包（core/services）的相对导入会报
    # "attempted relative import with no known parent package"
    module.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    module.__package__ = PACKAGE_NAME  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    # 关键：pytest importlib 模式会以模块名 "__init__" 导入 ROOT/__init__.py，
    # 预注册同名条目让那次导入命中缓存，不再二次执行顶层相对导入。
    sys.modules.setdefault("__init__", module)
    spec.loader.exec_module(module)
    return module


def pytest_collect_directory(path: Any, parent: Any) -> Any:
    """强制所有目录按 Dir 收集，不为插件根建 Package 节点（见模块 docstring）。"""
    from _pytest.nodes import Dir

    return Dir.from_parent(parent, path=path)


# ---------------------------------------------------------------------------
# 假宿主
# ---------------------------------------------------------------------------


@dataclass
class FakeConfig:
    """异常式配置通道（与真实 `self.config` 的行为一致）。"""

    data: dict[str, Any] = field(default_factory=dict)
    dump_error: Exception | None = None
    set_error: Exception | None = None
    writes: list[tuple[str, Any]] = field(default_factory=list)

    async def dump(self) -> dict[str, Any]:
        if self.dump_error is not None:
            raise self.dump_error
        return self.data

    async def set(self, path: str, value: Any) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.writes.append((path, value))
        section = path.split(".", 1)[0]
        key = path.split(".", 1)[-1]
        target = self.data.setdefault(section, {})
        if isinstance(target, dict):
            target[key] = value


@dataclass
class FakeStore:
    """Result 式存储通道；`available=False` 模拟 store 通道彻底不可用。"""

    data: dict[str, Any] = field(default_factory=dict)
    available: bool = True

    def _ok(self, value: Any) -> Any:
        return sys.modules["plugin.sdk.plugin"].Ok(value)

    def _err(self) -> Any:
        facade = sys.modules["plugin.sdk.plugin"]
        return facade.Err(facade.SdkError("store unavailable"))

    async def get(self, key: str, default: Any = None) -> Any:
        if not self.available:
            return self._err()
        return self._ok(self.data.get(key, default))

    async def set(self, key: str, value: Any) -> Any:
        if not self.available:
            return self._err()
        self.data[key] = value
        return self._ok(None)

    async def delete(self, key: str) -> Any:
        if not self.available:
            return self._err()
        return self._ok(self.data.pop(key, None) is not None)

    async def keys(self, prefix: str = "") -> Any:
        if not self.available:
            return self._err()
        return self._ok([key for key in self.data if key.startswith(prefix)])


class FakeBusNamespace:
    def __init__(self, records: list[dict[str, Any]] | None = None):
        self.records = list(records or [])

    async def get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.records)


class FakeBus:
    def __init__(self, records: list[dict[str, Any]] | None = None):
        self.conversations = FakeBusNamespace(records)


@dataclass
class FakeHostContext:
    """假宿主上下文：只带入口代码会碰的字段。"""

    plugin_id: str = "our_life"
    logger: Any = field(default_factory=lambda: logging.getLogger("our_life.test"))
    store: Any = field(default_factory=FakeStore)
    config: Any = field(default_factory=FakeConfig)
    bus: Any = field(default_factory=FakeBus)
    pushed: list[dict[str, Any]] = field(default_factory=list)


def build_plugin(host: FakeHostContext | None = None) -> tuple[Any, FakeHostContext]:
    """实例化插件主类并接管全部宿主通道，返回 (plugin, host)。

    **必须显式接管通道**（store / config / bus / logger / push_message）：
    测试要同时活在两种形态下——

    - 独立仓态：`plugin.sdk.plugin` 是桩，桩基类会照 `ctx` 把通道接过来；
    - 挂载态（市场 CI：`cp -R` 进宿主 `plugin/plugins/<id>`）：真 SDK 可用，
      真 `NekoPluginBase.__init__` 会**自己**建一套真通道（真 PluginStore / PluginConfig），
      一碰就 `TransportError: ctx.get_own_config is not available`。

    只在独立仓跑绿、挂载态全红，正是 release 门存在的意义；这里的接管让两种形态行为一致。
    """
    package = register_plugin_package()
    host = host or FakeHostContext()
    plugin = package.OurLifePlugin(host)
    plugin.logger = host.logger
    plugin.store = host.store
    plugin.config = host.config
    plugin.bus = host.bus
    plugin.push_message = _recording_push_message(host)
    return plugin, host


def _recording_push_message(host: FakeHostContext) -> Any:
    """把 push_message 换成记录桩（与真签名同构：**kwargs）。"""

    def _push(**kwargs: Any) -> dict[str, Any]:
        host.pushed.append(dict(kwargs))
        return {"submitted": True}

    return _push


def conversation_record(conversation_id: str, timestamp: float, lanlan: str, turn_type: str) -> dict[str, Any]:
    """构造一条总线轮次记录（形状与宿主 `bus.conversations` 一致）。"""
    return {
        "conversation_id": conversation_id,
        "timestamp": timestamp,
        "metadata": {"lanlan_name": lanlan, "turn_type": turn_type},
    }


_pre_register_parent_packages()
ensure_sdk_stub()
register_plugin_package()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def make_plugin() -> Any:
    """返回构造器：`make_plugin(config=..., records=..., store=...) -> (plugin, host)`。

    一律走 `build_plugin` 构造——它会显式接管宿主通道；直接 `OurLifePlugin(host)` 只在
    桩基类形态下碰巧能跑，挂载态（真基类）会静默换成真通道，导致本地全绿、CI 全红。
    """

    def _make(
        *,
        config: Any = None,
        store: Any = None,
        records: list[dict[str, Any]] | None = None,
    ) -> tuple[Any, FakeHostContext]:
        host = FakeHostContext(
            config=config if config is not None else FakeConfig(),
            store=store if store is not None else FakeStore(),
            bus=FakeBus(records),
        )
        return build_plugin(host)

    return _make


@pytest.fixture
def conversation() -> Any:
    """总线轮次记录的构造器。"""
    return conversation_record


@pytest.fixture
def run_async() -> Any:
    """把协程跑到底（不引入 pytest-asyncio 依赖）。"""

    def _run(coro: Any) -> Any:
        return asyncio.run(coro)

    return _run


@pytest.fixture
def make_config() -> Any:
    """构造异常式配置通道；测试文件不要 `from conftest import`，一律走夹具。"""

    def _make(**kwargs: Any) -> FakeConfig:
        return FakeConfig(**kwargs)

    return _make


@pytest.fixture
def make_store() -> Any:
    """构造 Result 式存储通道（`available=False` = 通道彻底不可用）。"""

    def _make(**kwargs: Any) -> FakeStore:
        return FakeStore(**kwargs)

    return _make
