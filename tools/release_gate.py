"""五门发版校验链：本地跑的就是 CI 跑的那一套。

门（顺序固定，任一失败即整链失败）::

    pytest   →  uv run python -m pytest tests -q           （cwd = 插件根）
    ruff     →  uvx ruff==0.12.4 的 CI 原样参数（--ignore-noqa，本仓已两次踩这个坑）
                先 `--offline` 走 uv 缓存（断网也照跑、版本与 CI 逐字一致），未命中才联网
    check    →  uv run neko-plugin check <插件源码目录>     （cwd = 宿主仓）
    release  →  真实 cp 进 <宿主>/plugin/plugins/<id>/，再按**裸 id** 跑 check -r
                （复刻市场 verify workflow：CI 就是挂载后按裸 id 校验）
    hosted-tsx → 把副本放进宿主仓点前缀探针目录跑面板检查
                （check-hosted-tsx 要求路径位于宿主仓内，所以只能走探针副本）

三条纪律（都来自台账里的真实踩坑）：

1. **独立仓本地全绿 ≠ CI 绿**。CI 是 ``cp -R`` 挂载后按裸 id 跑 ``check -r``，
   目录名断言/身份门在挂载态拿到的是裸 id。所以 release 门必须真的挂载一次。
2. **子进程管道与 stdout 必须钉 UTF-8**。Windows 默认是 GBK 码面，
   ``text=True`` 不钉编码会崩 reader 线程并丢掉整门日志。

3. **本地校验不许依赖网络**。ruff 门若必须先连 PyPI 才能拿到工具，断网时它先红，
   后面三门连跑都跑不到——报告会指向错误的方向。工具钉版本 + 离线优先才是对的。

3. **本脚本自己的 stdout 也必须兜住码面**。Windows 的 `sys.stdout.encoding` 默认是**活动代码页**
   （本机 GBK），且**输出被重定向时也一样**——后台任务 / 管道 / CI 捕获都会走进去。
   `print("全链通过 ✅")` 于是抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\u274c'`，
   整条链在"打印结果"这一步崩掉（门本身是好的，报告先死了）。本模块 import 时就按
   "是否 tty" 分两条路兜住：**重定向**（管道 / 捕获）一律重配 UTF-8 + `errors=replace`；
   **真控制台**保持原样、只降到 `errors=replace`（否则中文字节在 cp936 控制台会变乱码）。
   子进程另用 `PYTHONIOENCODING=utf-8` 钉死（见 `_run`），免得下游 gate 的日志被码面弄花。

用法::

    uv run python tools/release_gate.py                     # 全部五门
    uv run python tools/release_gate.py --only pytest,ruff  # 只跑子集
    uv run python tools/release_gate.py --keep              # 保留副本便于反复迭代
    uv run python tools/release_gate.py --host-root D:/other/N.E.K.O
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _harden_stdout() -> None:
    """兜住 stdout 的码面，别让"打印报告"这一步把整条链路带崩。

    Windows 下 `sys.stdout.encoding` 取的是活动代码页（本机 GBK），**被重定向时也一样**；
    `✅` / `❌` / `✓` 这些字符在 GBK 里没有码位，`print` 直接抛 UnicodeEncodeError。
    门本身跑得好好的，报告先死了——这是"本地全绿"的假象来源之一。

    分两条路（真控制台的码面不能乱改，否则中文变乱码）：
    - 重定向（管道 / 后台任务 / CI 捕获）：重配 UTF-8，让读取方能按 UTF-8 正确解码；
    - 真控制台：保持原编码，只把编不出的字符降级成 `?`，绝不抛异常。
    """
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
    except Exception:  # pragma: no cover - 取不到就按"非交互"处理
        is_tty = False
    try:
        if is_tty:
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover - 不支持的流就原样用
        pass


_harden_stdout()

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = PLUGIN_ROOT.name.removeprefix("n.e.k.o_plugin_")
REPO_NAME = f"n.e.k.o_plugin_{PLUGIN_ID}"

GATES = ("pytest", "ruff", "check", "release", "hosted-tsx")

# 复制插件时排除的东西：全是不该进发行包、也不该进副本的生成物。
_COPY_EXCLUDES = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
# 注意：`.vscode` 必须在副本内——它是 check -r 要求的仓库支撑文件，缺位即拒。
# ruff 必须**钉版本**：市场 CI 用的就是这一版，飘版本等于换了门。
_RUFF_VERSION = "0.12.4"
_RUFF_PACKAGE = f"ruff=={_RUFF_VERSION}"
# 跑 ruff 的工具：默认走 uvx（与 CI 同源），销路在 `_ruff_argv` 里。
_RUFF_RUNNER = "uvx"
_CI_RUFF_FLAGS = (
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


def resolve_host_root(explicit: str | None) -> Path:
    raw = explicit or os.environ.get("NEKO_HOST_ROOT") or str(PLUGIN_ROOT.parent / "N.E.K.O")
    host = Path(raw).expanduser().resolve()
    if not (host / "plugin" / "sdk").exists():
        raise SystemExit(
            f"[FAIL] 宿主仓定位失败：{host}\n"
            "  期望它是 N.E.K.O 宿主仓根（含 plugin/sdk）。用 --host-root 或 NEKO_HOST_ROOT 指定。"
        )
    return host


def _binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"[FAIL] 找不到可执行文件：{name}（请先安装并加入 PATH）")
    return found


def _ruff_argv() -> list[str]:
    """选一个能用的 ruff 启动方式，**绝不放低版本要求**。

    病因（真实踩到）：整条链原本固定用 `uvx ruff==<钉住的版本>`。`uvx` 每次都会去 PyPI
    解析一次——断网 / 代理不通时它重试三次后失败，于是 **ruff 门一红，后面三门根本没机会跑**，
    报告看起来却像"插件有问题"。

    修法：本机 uv 缓存里已经有这个钉住的版本时（`--offline` 命中），断网也照跑，且版本与
    CI 逐字一致。缓存没命中才让 uvx 联网解析一次（冷机上唯一需要联网的一次）。
    两条路都不通就明确报错并打印预热命令——**不能**顺手用 PATH 上那个版本未知的 `ruff`，
    那等于在没人知道的情况下换掉了门的版本。
    """
    uvx = shutil.which(_RUFF_RUNNER)
    if uvx:
        return [uvx, "--offline", _RUFF_PACKAGE, *_CI_RUFF_FLAGS]
    hint = (
        "[FAIL] 找不到可用的 ruff 启动方式。\n"
        f"  期望 {_RUFF_RUNNER} 在 PATH 上（离线依赖 uv 缓存里已有 {_RUFF_PACKAGE}）。\n"
        f"  预热缓存（需联网一次）：  {_RUFF_RUNNER} {_RUFF_PACKAGE} --version"
    )
    local = shutil.which("ruff")
    if not local:
        raise SystemExit(hint)
    found = _probe_version(local)
    if found != _RUFF_VERSION:
        raise SystemExit(
            f"{hint}\n"
            f"  PATH 上的 ruff 是 {found or '未知版本'}，与本门钉住的 {_RUFF_VERSION} 不一致，"
            "拒绝用它替代（否则等于悄悄换门）。"
        )
    return [local, *_CI_RUFF_FLAGS]


def _probe_version(executable: str) -> str:
    """问一下某个 ruff 可执行文件的版本；拿不到就返回空串。"""
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip().split()[-1] if proc.stdout.strip() else ""


def _run(cmd: list[str], cwd: Path, *, timeout: float = 1800.0) -> tuple[bool, str]:
    """跑一条子进程命令。stdout/stderr 必须钉 UTF-8（Windows GBK 码面会崩 reader 线程）。

    管道这一侧由 `encoding="utf-8"` 兜住，子进程那一侧由 `PYTHONIOENCODING=utf-8` 兜住——
    两边都钉住，日志才不会变成 `����ʱ` 这种读不出耗时的乱码。

    同时钉 `PYTHONDONTWRITEBYTECODE=1`：上游打包器的元数据探测会**以子进程 import 插件本体**，
    而 import 的目标是"已经过滤完的暂存树"，CPython 于是把 `__pycache__/*.pyc` 写进 payload，
    归档时一并入包（详见 `dist/upstream-issue-packager-pycache.md`：forever_companion v1.2.1
    因此多出 21 个 .pyc、体积 +73%）。探测子进程继承本进程环境，所以在**调用侧**设这个变量
    即可让产物干净，而且**不能事后删包里的 .pyc**——那会让 `metadata.toml` 里的 `payload.hash`
    与实际内容不符，宿主导入时校验不过。
    """
    started = time.monotonic()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"命令超时（>{timeout:.0f}s）：{' '.join(cmd)}"
    elapsed = time.monotonic() - started
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part and part.strip())
    tail = output.strip().splitlines()[-25:]
    body = "\n".join(f"    {line}" for line in tail)
    return proc.returncode == 0, f"{body}\n    （耗时 {elapsed:.1f}s，退出码 {proc.returncode}）"


def _copy_plugin(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(
        PLUGIN_ROOT,
        dest,
        ignore=shutil.ignore_patterns(*_COPY_EXCLUDES),
        dirs_exist_ok=False,
    )


def _cleanup(path: Path, *, keep: bool) -> None:
    if keep:
        print(f"    （--keep：保留副本 {path}）")
        return
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 各门
# ---------------------------------------------------------------------------


def gate_pytest(host_root: Path, keep: bool) -> tuple[bool, str]:
    return _run([_binary("uv"), "run", "python", "-m", "pytest", "tests", "-q"], PLUGIN_ROOT)


def _ruff_cached_offline() -> bool:
    """uv 缓存里是否已经有钉住版本的 ruff（有的话就能完全离线跑）。

    先用 `--version` 探一次是否可用；不可用时再跑 `pytest --collect-only` 拿到真实报错，
    只在报错**确实是"离线取不到工具"**时才回 False。

    为什么不能"离线这步失败就重试联网"：ruff 因**代码有问题**而退出码 1 也是"失败"，
    照那个逻辑就会把正常的 lint 失败当成缓存未命中——多跑一次联网、还打出一句
    误导人的"缓存未命中"，正是本模块要消灭的那类假信号。
    """
    uvx = shutil.which(_RUFF_RUNNER)
    if not uvx:
        return False
    probe = [uvx, "--offline", _RUFF_PACKAGE, "--version"]
    ok, _ = _run(probe, PLUGIN_ROOT, timeout=120.0)
    if ok:
        return True
    # --version 可能不被该版本接受：用 `--collect-only` 探，并只在"确实是离线取不到"时认输
    diagnostic = [uvx, "--offline", _RUFF_PACKAGE, "pytest", "--collect-only"]
    _, output = _run(diagnostic, PLUGIN_ROOT, timeout=300.0)
    lowered = output.casefold()
    return not ("offline" in lowered or "cache" in lowered or _RUFF_PACKAGE in lowered)


def gate_ruff(host_root: Path, keep: bool) -> tuple[bool, str]:
    """离线优先：缓存命中时断网也能跑，且版本与 CI 逐字一致。"""
    if not _ruff_cached_offline():
        print(f"    （uv 缓存里没有 {_RUFF_PACKAGE}，联网解析一次…）")
        argv = _ruff_argv()
        online = [part for part in argv if part != "--offline"]
        if "--offline" in argv:
            return _run(online, PLUGIN_ROOT)
        return _run(argv, PLUGIN_ROOT)
    return _run(_ruff_argv(), PLUGIN_ROOT)


def gate_check(host_root: Path, keep: bool) -> tuple[bool, str]:
    return _run(
        [_binary("uv"), "run", "neko-plugin", "check", str(PLUGIN_ROOT)],
        host_root,
    )


def gate_release(host_root: Path, keep: bool) -> tuple[bool, str]:
    """复刻市场 verify：挂载到 <宿主>/plugin/plugins/<裸 id>/，再按裸 id 跑 check -r。"""
    dest = host_root / "plugin" / "plugins" / PLUGIN_ID
    if dest.exists():
        return False, f"    挂载目标已存在，先手动清理：{dest}"
    try:
        _copy_plugin(dest)
        ok_sync, out_sync = _run(
            [_binary("uv"), "run", "--with", "pip", "neko-plugin", "sync", PLUGIN_ID, "--clean"],
            host_root,
        )
        if not ok_sync:
            return False, f"    sync 失败：\n{out_sync}"
        return _run(
            [_binary("uv"), "run", "neko-plugin", "check", "-r", PLUGIN_ID],
            host_root,
            timeout=1800.0,
        )
    finally:
        _cleanup(dest, keep=keep)


def gate_hosted_tsx(host_root: Path, keep: bool) -> tuple[bool, str]:
    """探针副本模式：check-hosted-tsx 要求被检查的插件位于宿主仓内。"""
    probe = host_root / "plugin" / "plugins" / f".{PLUGIN_ID}-gate-probe"
    manager = host_root / "frontend" / "plugin-manager"
    if not manager.exists():
        return False, f"    找不到 plugin-manager：{manager}"
    try:
        _copy_plugin(probe)
        relative = probe.relative_to(host_root).as_posix()
        return _run(
            [_binary("npm"), "run", "check-hosted-tsx", "--", relative],
            manager,
        )
    finally:
        _cleanup(probe, keep=keep)


_GATE_FUNCS = {
    "pytest": gate_pytest,
    "ruff": gate_ruff,
    "check": gate_check,
    "release": gate_release,
    "hosted-tsx": gate_hosted_tsx,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="our_life 五门发版校验链")
    parser.add_argument("--host-root", help=f"N.E.K.O 宿主仓根（默认 {PLUGIN_ROOT.parent / 'N.E.K.O'} 或 $NEKO_HOST_ROOT）")
    parser.add_argument("--only", help="只跑指定门（逗号分隔）：" + ",".join(GATES))
    parser.add_argument("--keep", action="store_true", help="保留宿主仓内的副本（默认无条件清理）")
    args = parser.parse_args()

    host_root = resolve_host_root(args.host_root)
    selected = GATES
    if args.only:
        requested = tuple(part.strip() for part in args.only.split(",") if part.strip())
        unknown = [name for name in requested if name not in _GATE_FUNCS]
        if unknown:
            raise SystemExit(f"[FAIL] 未知的门：{unknown}；可选：{', '.join(GATES)}")
        selected = requested

    print(f"plugin: {PLUGIN_ROOT}")
    print(f"host:   {host_root}")
    if PLUGIN_ID != "our_life":
        print(f"警告：目录名解析出的 plugin id 是 {PLUGIN_ID!r}，与预期 'our_life' 不一致")
    print()

    results: list[tuple[str, bool]] = []
    for name in selected:
        ok, output = _GATE_FUNCS[name](host_root, args.keep)
        mark = "OK " if ok else "FAIL"
        print(f"[{mark}] {name}")
        if output.strip():
            print(output)
        results.append((name, ok))
        if not ok:
            print("\n链路中断 ❌")
            return 1

    print("\n全链通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
