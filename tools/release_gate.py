"""五门发版校验链：本地跑的就是 CI 跑的那一套。

门（顺序固定，任一失败即整链失败）::

    pytest   →  uv run python -m pytest tests -q           （cwd = 插件根）
    ruff     →  uvx ruff==0.12.4 的 CI 原样参数（--ignore-noqa，本仓已两次踩这个坑）
    check    →  uv run neko-plugin check <插件源码目录>     （cwd = 宿主仓）
    release  →  真实 cp 进 <宿主>/plugin/plugins/<id>/，再按**裸 id** 跑 check -r
                （复刻市场 verify workflow：CI 就是挂载后按裸 id 校验）
    hosted-tsx → 把副本放进宿主仓点前缀探针目录跑面板检查
                （check-hosted-tsx 要求路径位于宿主仓内，所以只能走探针副本）

两条纪律（都来自台账里的真实踩坑）：

1. **独立仓本地全绿 ≠ CI 绿**。CI 是 ``cp -R`` 挂载后按裸 id 跑 ``check -r``，
   目录名断言/身份门在挂载态拿到的是裸 id。所以 release 门必须真的挂载一次。
2. **子进程管道与 stdout 必须钉 UTF-8**。Windows 默认是 GBK 码面，
   ``text=True`` 不钉编码会崩 reader 线程并丢掉整门日志。

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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = PLUGIN_ROOT.name.removeprefix("n.e.k.o_plugin_")
REPO_NAME = f"n.e.k.o_plugin_{PLUGIN_ID}"

GATES = ("pytest", "ruff", "check", "release", "hosted-tsx")

# 复制插件时排除的东西：全是不该进发行包、也不该进副本的生成物。
_COPY_EXCLUDES = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
# 注意：`.vscode` 必须在副本内——它是 check -r 要求的仓库支撑文件，缺位即拒。
_CI_RUFF = (
    "ruff==0.12.4",
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


def _run(cmd: list[str], cwd: Path, *, timeout: float = 1800.0) -> tuple[bool, str]:
    """跑一条子进程命令。stdout/stderr 必须钉 UTF-8（Windows GBK 码面会崩 reader 线程）。"""
    started = time.monotonic()
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


def gate_ruff(host_root: Path, keep: bool) -> tuple[bool, str]:
    return _run([_binary("uvx"), *_CI_RUFF], PLUGIN_ROOT)


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
