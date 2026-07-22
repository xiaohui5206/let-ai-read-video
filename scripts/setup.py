#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-watch 环境检查与安装（ffmpeg / ffprobe / yt-dlp + python 包）。

用法:
    python scripts/setup.py                          # 检查环境（默认，等价 --check）
    python scripts/setup.py --check
    python scripts/setup.py --install                # 下载缺失工具 + pip 安装包
    python scripts/setup.py --install --mirror cn    # pip 走清华镜像

检查对象:
    工具（<SKILL>/tools/ 优先，其次 PATH）：ffmpeg、ffprobe、yt-dlp
    python 包（当前解释器）：yt-dlp（模块 yt_dlp）、faster-whisper（模块 faster_whisper）

--install 行为:
    * yt-dlp 缺失        → 下载 GitHub releases 最新 yt-dlp.exe 到 tools/
    * ffmpeg/ffprobe 缺失 → 下载 gyan.dev ffmpeg-release-essentials.zip，
      只解压 bin/ffmpeg.exe、bin/ffprobe.exe 到 tools/（二者同包，缺谁解压谁）
    * python 包          → python -m pip install -U yt-dlp faster-whisper
    * 已在 tools/ 或 PATH 可用的工具不重复下载；模型权重不预下载
      （首次转写时由引擎自行缓存）。

RESULT_JSON 字段:
    ok / action / tools / packages / missing / ready（--install 时另有 installed）。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 任意 cwd 下可 import common
import common  # noqa: E402

YTDLP_EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
TSINGHUA_PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"

TOOLS = ("ffmpeg", "ffprobe", "yt-dlp")
PACKAGES = (("yt-dlp", "yt_dlp"), ("faster-whisper", "faster_whisper"))
# GPU 加速可选依赖（不装则自动回退 CPU，不影响 ready 判定）
CUDA_LIBS = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12")

UA = "video-watch-setup/1.0"


# ---------------------------------------------------------------- 检查


def _tool_version(path: str, tool: str) -> str | None:
    """尽力取版本首行；失败返回 None（不影响 found 判定）。"""
    flag = "--version" if tool == "yt-dlp" else "-version"
    try:
        proc = common.run([path, flag], timeout=30)
        lines = (proc.stdout or "").strip().splitlines()
        return lines[0].strip() if lines else None
    except Exception:
        return None


def check_tools() -> dict:
    """检查 ffmpeg/ffprobe/yt-dlp（tools/ 优先，其次 PATH），报告 found/path/version。"""
    out = {}
    for t in TOOLS:
        p = common.find_tool(t)
        entry = {"found": p is not None, "path": p}
        if p:
            entry["version"] = _tool_version(p, t)
        out[t] = entry
    return out


def check_packages() -> dict:
    """检查 python 包（find_spec 探测，不 import 本体），报告 installed/version。"""
    out = {}
    for dist, mod in PACKAGES:
        try:
            installed = importlib.util.find_spec(mod) is not None
        except Exception:
            installed = False
        entry = {"installed": installed}
        if installed:
            try:
                entry["version"] = importlib.metadata.version(dist)
            except Exception:
                entry["version"] = None
        out[dist] = entry
    return out


def build_missing(tools: dict, packages: dict) -> list[str]:
    """缺失清单：工具用原名（ffmpeg/ffprobe/yt-dlp），python 包加 `pip:` 前缀以区分。"""
    missing = [t for t, st in tools.items() if not st.get("found")]
    missing += [f"pip:{d}" for d, st in packages.items() if not st.get("installed")]
    return missing


def check_gpu() -> dict:
    """GPU 加速就绪情况（信息性字段，不计入 missing）：

    - cuda_libs：pip 版 CUDA 库是否安装（ctranslate2 GPU 运行所需）
    - cuda_devices：装了库之后实际可见的 CUDA 设备数（检测前需把 nvidia/*/bin 前置 PATH）
    """
    libs = {}
    for dist in CUDA_LIBS:
        try:
            libs[dist] = {"installed": True, "version": importlib.metadata.version(dist)}
        except Exception:
            libs[dist] = {"installed": False, "version": None}
    devices = 0
    if all(st["installed"] for st in libs.values()):
        try:
            import glob as _glob
            import os as _os
            import site as _site
            for base in set(_site.getsitepackages() + [_site.getusersitepackages()]):
                for d in _glob.glob(_os.path.join(base, "nvidia", "*", "bin")):
                    if _os.path.isdir(d):
                        _os.environ["PATH"] = d + _os.pathsep + _os.environ.get("PATH", "")
            import ctranslate2  # noqa
            devices = ctranslate2.get_cuda_device_count()
        except Exception:
            devices = 0
    return {"cuda_libs": libs, "cuda_devices": devices,
            "hint": None if devices else "未启用 GPU：无 N 卡可忽略；有 N 卡可运行 "
            "`python scripts/setup.py --install --with-cuda` 安装 CUDA 库（转写提速 10~50 倍）"}


# ---------------------------------------------------------------- 安装


def _download(url: str, dest: Path) -> None:
    """流式下载 url → dest（先写 .part 再改名，避免半成品），带简单进度日志。"""
    common.log(f"下载 {url}\n  -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            done = 0
            mark = 20 * 1024 * 1024
            while True:
                chunk = resp.read(512 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done >= mark:
                    common.log(f"  已下载 {done / 1048576:.0f} MB ...")
                    mark += 50 * 1024 * 1024
        tmp.replace(dest)
        common.log(f"  完成，共 {dest.stat().st_size / 1048576:.1f} MB")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def install_tools(tools_status: dict) -> list[str]:
    """把缺失的便携工具下载到 <SKILL>/tools/，返回本次下载的工具名列表。"""
    tdir = common.tools_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    done: list[str] = []

    # 1) yt-dlp.exe（GitHub releases latest）
    if not tools_status["yt-dlp"]["found"]:
        _download(YTDLP_EXE_URL, tdir / "yt-dlp.exe")
        done.append("yt-dlp")
    else:
        common.log("yt-dlp 已可用，跳过便携版下载")

    # 2) ffmpeg / ffprobe（gyan.dev essentials zip，二者同包）
    need = [t for t in ("ffmpeg", "ffprobe") if not tools_status[t]["found"]]
    if need:
        with tempfile.TemporaryDirectory(prefix="vw-ffmpeg-") as td:
            zpath = Path(td) / "ffmpeg-release-essentials.zip"
            _download(FFMPEG_ZIP_URL, zpath)
            with zipfile.ZipFile(zpath) as zf:
                names = zf.namelist()
                for tool in need:
                    suffix = f"bin/{tool}.exe"  # zip 内路径如 ffmpeg-*-essentials_build/bin/ffmpeg.exe
                    match = next((n for n in names if n.endswith(suffix)), None)
                    if not match:
                        raise RuntimeError(f"ffmpeg 压缩包内未找到 {suffix}")
                    dest = tdir / f"{tool}.exe"
                    with zf.open(match) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 512)
                    common.log(f"解压 {match} -> {dest}")
                    done.append(tool)
    else:
        common.log("ffmpeg/ffprobe 已可用，跳过便携版下载")
    return done


def pip_install(mirror: str | None, with_cuda: bool = False) -> list[str]:
    """pip 安装/升级 python 包（--mirror cn 加清华镜像），返回安装的包名列表。"""
    targets = ["yt-dlp", "faster-whisper"]
    if with_cuda:
        targets += list(CUDA_LIBS)
    cmd = [sys.executable, "-m", "pip", "install", "-U"] + targets
    if mirror == "cn":
        cmd += ["-i", TSINGHUA_PYPI]
    common.log("安装/升级 python 包: " + " ".join(cmd))
    proc = common.run(cmd, timeout=1800, check=False)
    for line in (proc.stdout or "").strip().splitlines()[-10:]:
        common.log("  pip: " + line)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"pip 安装失败 exit={proc.returncode}:\n{tail}")
    return targets


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(
        prog="setup.py",
        description="video-watch 环境检查与安装：ffmpeg/ffprobe/yt-dlp（<SKILL>/tools/ 优先、PATH 其次）"
                    " + python 包 yt-dlp、faster-whisper。模型权重不预下载。",
        epilog="RESULT_JSON 字段: ok, action, tools{ffmpeg,ffprobe,yt-dlp:{found,path,version}}, "
               "packages{yt-dlp,faster-whisper:{installed,version}}, missing(工具原名/pip:包名), "
               "ready(missing 为空), installed(仅 --install)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="只检查环境并报告（默认行为）")
    g.add_argument("--install", action="store_true",
                   help="下载缺失的便携 yt-dlp/ffmpeg/ffprobe 到 tools/，并 pip 安装/升级 yt-dlp、faster-whisper")
    ap.add_argument("--mirror", choices=["cn"], default=None,
                    help="pip 使用清华镜像 https://pypi.tuna.tsinghua.edu.cn/simple（仅 --install 时生效）")
    ap.add_argument("--with-cuda", action="store_true",
                    help="--install 时追加安装 GPU 加速库 nvidia-cublas-cu12 + nvidia-cudnn-cu12"
                         "（有 NVIDIA 显卡时用，转写提速 10~50 倍；无 N 卡不必装）")
    args = ap.parse_args(argv)

    try:
        tools = check_tools()
        packages = check_packages()
        installed = None

        if args.install:
            downloaded = install_tools(tools)
            pkg_targets = pip_install(args.mirror, with_cuda=args.with_cuda)
            installed = {"tools": downloaded, "packages": pkg_targets}
            common.log("安装完成，复查环境 ...")
            tools = check_tools()
            packages = check_packages()

        missing = build_missing(tools, packages)
        result = {
            "ok": True,
            "action": "install" if args.install else "check",
            "tools": tools,
            "packages": packages,
            "gpu": check_gpu(),
            "missing": missing,
            "ready": not missing,
        }
        if installed is not None:
            result["installed"] = installed
        common.print_result(result)
        return 0
    except Exception as e:  # 统一走契约错误通道
        common.die(str(e))


if __name__ == "__main__":
    sys.exit(main())
