#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-watch 共享函数库与 CLI 输出协议。

各 scripts/*.py 通过以下方式导入（脚本在任意 cwd 下均可运行）::

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common

通用契约：
1. 每个 CLI 脚本 stdout 的最后一行必须是 `RESULT_JSON: {...}`（单行 JSON），
   日志一律在此之前用 log() 打印；出错用 die()：stderr 写详情、
   stdout 末行 `RESULT_JSON: {"ok": false, "error": ...}`、退出码 1。
2. 外部工具（ffmpeg/ffprobe/yt-dlp）一律经 find_tool() 查找：
   <SKILL>/tools/<name>.exe 优先，PATH 其次。
3. 时间参数用 parse_time() 解析（秒 / MM:SS / HH:MM:SS），
   展示用 fmt_ts()（MM:SS，满 1 小时用 HH:MM:SS）。
4. 仅 Python 3.10+ stdlib；本模块不 import 任何第三方包。

设计说明（一次性解释各脚本头部的重复兜底块）：
每个 CLI 脚本头部都内嵌了一份本模块的"最小兜底实现"（约 40 行，
导入 common 失败时启用）。这是**有意为之的取舍**而非疏漏：
skill 会以整个文件夹的形式被拷贝到各种 AI Agent 的运行环境中，
任一脚本都可能被单独抽取出来执行；内嵌兜底可保证脚本在
common.py 缺失或被单独拷贝时仍可独立运行。兜底实现只覆盖公共接口
（log/die/find_tool/parse_time/fmt_ts/RESULT_JSON），接口不变则无需
与各脚本同步；新增功能请只加在本模块中。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "skill_root", "tools_dir", "runs_dir", "find_tool", "run",
    "parse_time", "fmt_ts", "frame_budget", "slugify",
    "redact_url", "redact_text_urls",
    "print_result", "die", "log", "setup_stdio", "now_stamp",
]

# ---------------------------------------------------------------- 路径


def skill_root() -> Path:
    """skill 根目录 = scripts/ 的上一级。"""
    return Path(__file__).resolve().parent.parent


def tools_dir() -> Path:
    """便携工具目录 <SKILL>/tools。只返回路径，不创建（由 setup.py --install 创建）。"""
    return skill_root() / "tools"


def runs_dir() -> Path:
    """运行产物根目录 <SKILL>/runs。只返回路径，不创建（由 watch.py 等按需创建）。"""
    return skill_root() / "runs"


# ---------------------------------------------------------------- 隐私

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)


def redact_url(value: str) -> str:
    """移除 HTTP(S) URL 中的凭据、查询参数和片段，供日志与标题兜底使用。"""
    raw = str(value)
    try:
        parts = urlsplit(raw)
        if parts.scheme.lower() not in {"http", "https"}:
            return raw
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return "<redacted-url>" if raw.lower().startswith(("http://", "https://")) else raw


def redact_text_urls(value: object) -> str:
    """清理诊断文本中出现的所有 HTTP(S) URL。"""
    return _HTTP_URL_RE.sub(lambda match: redact_url(match.group(0)), str(value))


def find_tool(name: str) -> str | None:
    """查找可执行工具：<SKILL>/tools/<name>.exe → <SKILL>/tools/<name> → PATH。

    name 不带扩展名（如 "ffmpeg"）。返回路径 str；未找到返回 None。
    """
    td = tools_dir()
    for cand in (td / f"{name}.exe", td / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)  # PATH 查找（Windows 经 PATHEXT 自动匹配 .exe）


# ---------------------------------------------------------------- 子进程


def run(cmd, timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess:
    """运行外部命令并捕获输出（契约：list 形式，禁用 shell=True）。

    - stdout / stderr 均按 UTF-8 解码、errors='replace'；
    - check=True（默认）时非零退出码抛 RuntimeError（附 stderr/stdout 尾部 500 字）；
      check=False 时由调用方自行判断 proc.returncode；
    - 可执行文件不存在 / 超时同样抛 RuntimeError；
    - 返回 subprocess.CompletedProcess，.stdout/.stderr 为 str。
    """
    if isinstance(cmd, str):
        raise TypeError("run() 只接受 list 形式的 cmd（禁止 shell 字符串）")
    argv = [str(c) for c in cmd]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(f"可执行文件不存在: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时（>{timeout}s）: {argv[0]}") from None
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"命令失败 exit={proc.returncode}: {argv[0]}\n{tail}")
    return proc


# ---------------------------------------------------------------- 时间

# "MM:SS" 或 "HH:MM:SS"；无 HH 时 MM 允许 >59（如 "75:00"），秒可带小数
_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d+):([0-5]?\d(?:\.\d+)?)$")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_time(s) -> float:
    """解析时间 → 秒(float)。接受：秒(数字或数字字符串) / "MM:SS" / "HH:MM:SS"。

    parse_time("90") == parse_time("01:30") == parse_time(90) == 90.0
    非法输入抛 ValueError。
    """
    if isinstance(s, (int, float)):
        v = float(s)
        if v < 0:
            raise ValueError(f"时间不能为负: {s}")
        return v
    text = str(s).strip()
    if not text:
        raise ValueError("时间字符串为空")
    if _NUM_RE.fullmatch(text):
        return float(text)
    m = _CLOCK_RE.match(text)
    if not m:
        raise ValueError(f"无法解析时间 {s!r}；支持 秒 / MM:SS / HH:MM:SS")
    hh_s, mm_s, ss_s = m.groups()
    mm = int(mm_s)
    if hh_s is not None and mm >= 60:
        raise ValueError(f"HH:MM:SS 中 MM 须 <60: {s!r}")
    return int(hh_s or 0) * 3600 + mm * 60 + float(ss_s)


def fmt_ts(seconds: float) -> str:
    """时间戳显示：MM:SS；≥1 小时用 HH:MM:SS（秒先四舍五入到整数）。"""
    total = max(0, int(round(float(seconds))))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ---------------------------------------------------------------- 帧预算


def frame_budget(duration: float, start=None, end=None) -> int:
    """抽帧预算（目标帧数上限）。

    - 整片（start/end 均为空）：时长 ≤30s→30，≤60s→40，≤3min→60，≤10min→80，>10min→100；
    - 聚焦窗口（给了 start 或 end，缺省端点补 0 / duration）：
      窗口秒数 × 1fps 密度，下限 20、上限 100；
    - 全局硬上限：按作用时长（整片=duration，窗口=窗口长）折算 2fps，且 ≤100 帧；
      硬上限最后应用，结果至少为 1。
    """
    d = max(0.0, float(duration or 0.0))
    if start is None and end is None:
        span = d
        if d <= 30:
            n = 30
        elif d <= 60:
            n = 40
        elif d <= 180:
            n = 60
        elif d <= 600:
            n = 80
        else:
            n = 100
    else:
        s0 = float(start) if start is not None else 0.0
        e0 = float(end) if end is not None else d
        span = max(0.0, e0 - s0)
        n = int(span)  # 1fps 密度
        n = max(20, min(100, n))
    hard = int(span * 2)  # 2fps 硬上限
    return max(1, min(n, hard, 100))


# ---------------------------------------------------------------- 文本


def slugify(text: str, max_len: int = 40) -> str:
    """由标题/文件名生成 run 目录 slug。

    NFKC 规范化、ASCII 转小写，保留中英文数字，其余字符折叠为单个 '-'，
    去首尾 '-'，最长 max_len 字符；空结果回退 "video"。
    （调用方若传文件名请自行去扩展名，本函数不特殊处理扩展名。）
    """
    if not text:
        return "video"
    norm = unicodedata.normalize("NFKC", str(text)).lower()
    out: list[str] = []
    pending_dash = False
    for ch in norm:
        if ch.isalnum():  # 中文/英文/数字均为 True
            if pending_dash and out:
                out.append("-")
            pending_dash = False
            out.append(ch)
        else:
            pending_dash = True
    slug = "".join(out)[:max_len].rstrip("-")
    return slug or "video"


def now_stamp() -> str:
    """本地时间戳 yyyymmdd_hhmmss（run 目录命名用）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------- 输出


def setup_stdio() -> None:
    """stdout/stderr 重新配置为 UTF-8 + errors='replace'（防 Windows 控制台中文报错）。

    每个 CLI 脚本 main() 开头调用一次；重复调用安全。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def log(msg: str) -> None:
    """日志：打在 stdout，位于最终 RESULT_JSON 行之前（契约"日志打前面"）。"""
    print(f"[video-watch] {msg}", flush=True)


def print_result(d: dict) -> None:
    """在 stdout 最后一行输出单行 JSON：`RESULT_JSON: {...}`。必须是脚本最后一次输出。

    ensure_ascii=False 保留中文；default=str 容错 Path/日期等不可序列化对象。
    """
    print("RESULT_JSON: " + json.dumps(d, ensure_ascii=False, default=str), flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    """出错：stderr 写详情，stdout 末行 RESULT_JSON {"ok": false, ...}，退出码 1。"""
    print(f"[video-watch][ERROR] {msg}", file=sys.stderr, flush=True)
    print_result({"ok": False, "error": str(msg)})
    raise SystemExit(code)


# ---------------------------------------------------------------- 自检


if __name__ == "__main__":
    # 自检入口：python scripts/common.py（不联网、不安装，仅打印路径与函数抽样结果）
    setup_stdio()
    _info = {
        "ok": True,
        "skill_root": str(skill_root()),
        "tools_dir": str(tools_dir()),
        "runs_dir": str(runs_dir()),
        "tools": {n: find_tool(n) for n in ("ffmpeg", "ffprobe", "yt-dlp")},
        "selftest": {
            "parse_time('01:30')": parse_time("01:30"),
            "parse_time('1:02:03')": parse_time("1:02:03"),
            "fmt_ts(3723)": fmt_ts(3723),
            "frame_budget(45)": frame_budget(45),
            "frame_budget(7200)": frame_budget(7200),
            "frame_budget(300, 10, 70)": frame_budget(300, 10, 70),
            "slugify('示例 Video 标题.mp4')": slugify("示例 Video 标题.mp4"),
            "now_stamp()": now_stamp(),
        },
    }
    print_result(_info)
