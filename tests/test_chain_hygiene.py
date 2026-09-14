"""发版链自身卫生门（常驻）。

`tools/` 不在发行包里，但它决定"改完有没有全绿"这个判断本身可不可信——
它自己坏掉的时候，红的是**报告**，不是代码，于是维护者会去修一个根本不存在的 bug。
这一组门盯的就是这类"门比被测物先死"的故障：

1. **stdout 码面**：Windows 的 `sys.stdout.encoding` 默认取活动代码页（本机 GBK），
   **被重定向时也一样**。`print("全链通过 ✅")` 于是抛 `UnicodeEncodeError`，
   整条链在"打印结果"这一步崩掉——门是好的，报告先死了。
   真机上就是这样：`pytest` / `check` 都 OK，退在 `print` 上。
2. **离线可跑**：`uvx ruff==<钉住版本>` 每次都会去 PyPI 解析一次，断网时重试三次后失败，
   于是 ruff 门先红、**后面三门连跑都跑不到**。工具钉版本 + 离线优先才是对的。
   这条门同时钉住"版本不许飘"——降级到 PATH 上任意版本的 ruff 等于偷偷换门。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "tools" / "release_gate.py"


def _load_gate() -> Any:
    """按文件路径加载 `tools/release_gate.py`（`tools/` 不是包，也不是插件模块）。

    用 `spec_from_file_location` 而不是裸 import：仓根被 conftest 注册成了包 `our_life`，
    而 `tools/` 与它无关；按路径加载也顺带保证门测的是真实脚本文件本身。
    """
    spec = importlib.util.spec_from_file_location("our_life_release_gate_under_test", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. stdout 码面
# ---------------------------------------------------------------------------


class _FakeStream:
    """最小 stdout 替身：记录 `reconfigure` 的实参，`isatty` 可指定。"""

    def __init__(self, *, tty: bool, reconfigure_raises: bool = False):
        self._tty = tty
        self.calls: list[dict[str, Any]] = []
        self._raises = reconfigure_raises

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs: Any) -> None:
        if self._raises:
            raise ValueError("stream does not support reconfiguration")
        self.calls.append(kwargs)


def test_hardening_pins_utf8_when_stdout_is_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    """被重定向（管道 / 后台任务 / CI 捕获）时必须重配 UTF-8，否则报告会崩在 print 上。"""
    gate = _load_gate()
    stream = _FakeStream(tty=False)
    monkeypatch.setattr(sys, "stdout", stream)

    gate._harden_stdout()

    assert len(stream.calls) == 1, f"应当只重配一次，实际 {stream.calls}"
    assert stream.calls[0]["encoding"] == "utf-8"
    # errors=replace 是第二道保险：万一还有编不出的字符，宁可降级成 '?' 也不许抛异常
    assert stream.calls[0]["errors"] == "replace"


def test_hardening_keeps_console_encoding_but_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """真控制台的码面不能乱改（否则中文变乱码），只把编不出的字符降级。"""
    gate = _load_gate()
    stream = _FakeStream(tty=True)
    monkeypatch.setattr(sys, "stdout", stream)

    gate._harden_stdout()

    assert len(stream.calls) == 1
    assert "encoding" not in stream.calls[0], "真控制台不该改编码"
    assert stream.calls[0]["errors"] == "replace"


def test_hardening_survives_a_stream_that_cannot_be_reconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到 reconfigure / 重配失败都不许把整个脚本带崩。"""
    gate = _load_gate()
    raising = _FakeStream(tty=False, reconfigure_raises=True)
    monkeypatch.setattr(sys, "stdout", raising)
    gate._harden_stdout()  # 不抛就算过

    class _NoReconfigure:
        pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    gate._harden_stdout()  # 不抛就算过

    monkeypatch.delattr(sys, "stdout", raising=False)
    gate._harden_stdout()  # stdout 为 None 也不许崩


# 子进程里干两件事：① 按真实用法加载门脚本（触发模块级 `_harden_stdout()`）；
# ② 走一遍 `main()` 的两条**打印路径**。故意不真跑门——那会让本测试递归地把整个套件
# 再跑一次（真机上实测 90 秒自噬并失败）。
_PRINT_PATH_SCRIPT = """
import importlib.util
import sys

target = sys.argv[1]
spec = importlib.util.spec_from_file_location("our_life_gate_child", target)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

print(module.PLUGIN_ID)
print("\\n全链通过 ✅")
print("\\n链路中断 ❌")
print("stdout encoding:", getattr(sys.stdout, "encoding", None))
"""


def test_gate_prints_success_marker_under_a_redirected_pipe() -> None:
    """端到端复现门：**真**用管道捕获 stdout 走一遍 `main()` 的打印路径，必须不崩。

    这就是真机上崩掉的那个形态——门自己 OK，脚本却退在 `print("全链通过 ✅")` 上
    （`'gbk' codec can't encode character '\\u2705'`：`sys.stdout.encoding` 是活动代码页 GBK，
    被重定向时也一样）。子进程故意**不给** `PYTHONIOENCODING`，好让它复现原始环境。

    之所以只走打印路径、不真跑门：本测试自身就在 pytest 里，真跑一次 `--only pytest`
    会把整个套件递归再跑一遍（实测 90 秒自噬并失败）。打印路径与门内容无关，单独验足够了。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    proc = subprocess.run(
        [sys.executable, "-c", _PRINT_PATH_SCRIPT, str(GATE_PATH)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    assert "UnicodeEncodeError" not in proc.stderr, f"报告打印崩了：\n{proc.stderr[-800:]}"
    assert proc.returncode == 0, f"退出码 {proc.returncode}：\n{proc.stderr[-800:]}"
    assert "our_life" in proc.stdout, f"子进程没加载到门脚本：\n{proc.stdout[-500:]}"
    assert "全链通过" in proc.stdout, f"没看到成功标记：\n{proc.stdout[-500:]}"
    assert "链路中断" in proc.stdout, f"没看到失败标记：\n{proc.stdout[-500:]}"
    assert "utf-8" in proc.stdout, f"重定向时应当被重配成 UTF-8：\n{proc.stdout[-500:]}"


# ---------------------------------------------------------------------------
# 2. 离线可跑 + 工具版本不飘
# ---------------------------------------------------------------------------


def test_ruff_is_offline_first_and_version_pinned() -> None:
    """ruff 门必须离线优先，且版本逐字钉死（等于 CI 那一版）。"""
    gate = _load_gate()
    assert gate._RUFF_PACKAGE == f"ruff=={gate._RUFF_VERSION}"

    import shutil

    if shutil.which(gate._RUFF_RUNNER) is None:
        pytest.skip(f"{gate._RUFF_RUNNER} 不在 PATH 上")

    argv = gate._ruff_argv()
    assert argv[0] == shutil.which(gate._RUFF_RUNNER)
    assert "--offline" in argv, "必须离线优先，否则断网时 ruff 门先红、后面三门跑不到"
    assert gate._RUFF_PACKAGE in argv
    # CI 原样的检查参数一个都不能少
    for flag in ("--ignore-noqa", "--isolated", "--target-version", "--select", "."):
        assert flag in argv


def test_ruff_flags_stay_identical_to_the_ci_invocation() -> None:
    """参数与 README 里写的那条 CI 命令逐字一致（`--ignore-noqa` 是本仓踩过两次的坑）。"""
    gate = _load_gate()
    assert gate._CI_RUFF_FLAGS == (
        "check",
        "--ignore-noqa",
        "--isolated",
        "--target-version",
        "py311",
        "--line-length",
        "120",
        "--select",
        "E4,E7,E9,F,I",
        "--exclude",
        "vendor",
        ".",
    )


def test_offline_probe_hit_does_not_misreport_a_lint_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """**代码有问题**和**工具没缓存**是两回事，不许混成一句"缓存未命中"。

    病因（本轮实测踩到）：第一版写法是"离线跑一次，失败就当缓存未命中、再联网跑一次"。
    可 ruff 因为代码里有 lint 错误而退出码 1 也是"失败"——于是正常的 lint 失败被当成
    缓存未命中，多跑一次联网、还打出一句误导人的 `（离线缓存未命中，联网解析一次…）`。
    报告里的假信号比门本身红更坏：它会把人带去查网络。

    这条门在**真的**用一个隔离目录里的 lint 违规去逼 ruff 退出码 1，然后断言
    ① 门确实红了、② 红的原因是 lint 而不是"缓存未命中"。
    """
    gate = _load_gate()
    import shutil

    if shutil.which(gate._RUFF_RUNNER) is None:
        pytest.skip(f"{gate._RUFF_RUNNER} 不在 PATH 上")
    if not gate._ruff_cached_offline():
        pytest.skip(f"uv 缓存里没有 {gate._RUFF_PACKAGE}（离线不可用）")

    with tempfile.TemporaryDirectory() as raw:
        sandbox = Path(raw)
        # 故意写一个 F401（`--select ... F` 会红），且放在独立目录里，不污染本仓
        (sandbox / "sample.py").write_text("import os\n", encoding="utf-8")
        original = gate.PLUGIN_ROOT
        gate.PLUGIN_ROOT = sandbox
        try:
            ok, output = gate.gate_ruff(sandbox, keep=False)
        finally:
            gate.PLUGIN_ROOT = original

    captured = capsys.readouterr()
    assert ok is False, "F401 应当让 ruff 门红"
    assert "F401" in output, f"应当是真的 lint 报错：\n{output}"
    assert "缓存" not in captured.out, f"lint 失败被误报成缓存未命中：\n{captured.out}"


def test_gate_never_silently_falls_back_to_an_unpinned_ruff() -> None:
    """PATH 上那个版本不一致的 ruff 不许被顶上来用；顶上来就等于换了门。"""
    gate = _load_gate()
    source = GATE_PATH.read_text(encoding="utf-8")
    assert "_probe_version" in source, "需要一个版本核对手段"
    assert "_RUFF_VERSION" in source

    # 走一遍"uvx 不在 PATH"的分支：若 PATH 上那个 ruff 版本对不上，必须直接报错退出。
    import shutil

    original_which = shutil.which

    def fake_which(name: str) -> str | None:
        if name == gate._RUFF_RUNNER:
            return None
        return original_which(name)

    shutil.which = fake_which  # type: ignore[assignment]
    try:
        local = shutil.which("ruff")
        if local is None:
            with pytest.raises(SystemExit):
                gate._ruff_argv()
        else:
            found = gate._probe_version(local)
            if found == gate._RUFF_VERSION:
                assert gate._ruff_argv() == [local, *gate._CI_RUFF_FLAGS]
            else:
                with pytest.raises(SystemExit) as excinfo:
                    gate._ruff_argv()
                assert "拒绝用它替代" in str(excinfo.value) or "版本" in str(excinfo.value)
    finally:
        shutil.which = original_which  # type: ignore[assignment]
