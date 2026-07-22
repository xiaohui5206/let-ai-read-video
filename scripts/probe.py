#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py — 探测视频元数据（不下载视频内容）。

用法:
    python scripts/probe.py --input <视频URL或本地文件路径>

行为:
  * 本地文件: 调用 ffprobe（tools/ 优先，其次 PATH）解析时长/宽高/帧率/音轨。
  * URL: 用 yt_dlp 仅取元数据（title/duration/width/height/fps/字幕列表）。
  * captions 合并手动字幕(subtitles)与自动字幕(automatic_captions)，手动优先标注；
    同一语言两种都有时只记 manual。
  * needs_transcribe = has_audio and not captions
  * suggested_budget 走 common.frame_budget（整片预算，无聚焦窗口）。

约定:
  * 日志打在 stdout 前面；最后一行 stdout 固定输出单行 JSON:  RESULT_JSON: {...}
  * 出错写 stderr、退出码 1，最后一行 RESULT_JSON: {"ok": false, "error": "..."}
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础环境：Windows 控制台强制 UTF-8（无法编码的字符替换而非崩溃）
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent   # skill 根目录
SCRIPTS_DIR = Path(__file__).resolve().parent         # scripts/ 目录
RESULT_PREFIX = "RESULT_JSON: "


def log(msg: str) -> None:
    """普通日志，打在 RESULT_JSON 之前。"""
    print(msg, flush=True)


def emit(payload: dict) -> None:
    """最后一行 stdout：单行 JSON 结果（供调用方解析）。"""
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def fail(msg: str) -> None:
    """统一错误出口：stderr + RESULT_JSON(ok=false) + 退出码 1。"""
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    emit({"ok": False, "error": msg})
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 复用 common（冻结契约：find_tool / frame_budget）。common.py 缺失时启用
# 与契约一致的最小兜底实现，保证 probe.py 仍可独立运行。
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from common import find_tool, frame_budget  # type: ignore
except Exception:  # pragma: no cover - 兜底分支

    def find_tool(name):
        """先 <SKILL>/tools/<name>.exe，再 PATH；找不到返回 None。"""
        exe = name if name.lower().endswith(".exe") else f"{name}.exe"
        cand = SKILL_ROOT / "tools" / exe
        if cand.is_file():
            return str(cand)
        return shutil.which(name) or shutil.which(exe)

    def frame_budget(duration, start=None, end=None):
        """整片: ≤30s→30 / ≤60s→40 / ≤3min→60 / ≤10min→80 / >10min→100；
        聚焦窗口: 1fps 密度、下限 20、上限 100（硬上限 2fps、100 帧）。"""
        try:
            dur = float(duration or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if start is not None or end is not None:
            s = float(start or 0)
            e = float(end) if end is not None else dur
            win = max(0.0, min(e, dur) - s) if dur > 0 else max(0.0, e - s)
            return max(20, min(100, int(win * 1.0)))
        if dur <= 30:
            return 30
        if dur <= 60:
            return 40
        if dur <= 180:
            return 60
        if dur <= 600:
            return 80
        return 100


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def to_float(v):
    """宽松转 float；None/非法/NaN → None。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # NaN 检查
    except (TypeError, ValueError):
        return None


def parse_fps(rate):
    """ffprobe 帧率（如 '30000/1001'）→ float；非法 → None。"""
    if not rate or str(rate) in ("0/0", "N/A"):
        return None
    try:
        s = str(rate)
        if "/" in s:
            num, den = s.split("/", 1)
            num, den = float(num), float(den)
            if den == 0:
                return None
            return round(num / den, 3)
        return round(float(s), 3)
    except (TypeError, ValueError):
        return None


def is_url(s: str) -> bool:
    """仅把 http(s) 视为 URL（yt_dlp 也只对这类链接取元数据）。"""
    return s.startswith("http://") or s.startswith("https://")


def lang_rank(lang):
    """字幕语言排序键：zh-Hans > zh-Hant > 其他 zh* > en* > 其他（字母序）。"""
    ll = str(lang).lower()
    for i, p in enumerate(("zh-hans", "zh-hant", "zh", "en")):
        if ll.startswith(p):
            return (i, ll)
    return (9, ll)


# ---------------------------------------------------------------------------
# 本地文件：ffprobe
# ---------------------------------------------------------------------------
def probe_file(path: Path) -> dict:
    if not path.exists():
        fail(f"本地文件不存在: {path}")
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        fail("未找到 ffprobe：请先运行 python scripts/setup.py --install，"
             "或将 ffprobe 放入 tools/ 目录或 PATH")

    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    log(f"ffprobe 探测本地文件: {path}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        fail("ffprobe 执行超时（120s）")
    except OSError as e:
        fail(f"ffprobe 启动失败: {e}")
    if proc.returncode != 0:
        fail(f"ffprobe 执行失败: {(proc.stderr or '').strip()[:400] or '未知错误'}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        fail(f"ffprobe 输出不是合法 JSON: {e}")

    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    # 第一个“真”视频流（排除封面图 attached_pic），与第一条音频流
    vstream = next((s for s in streams
                    if s.get("codec_type") == "video"
                    and s.get("width")
                    and (s.get("disposition") or {}).get("attached_pic", 0) == 0), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # 时长：format 级优先，退化为流级最大值
    duration = to_float(fmt.get("duration"))
    if duration is None:
        cands = [to_float(s.get("duration")) for s in streams]
        cands = [c for c in cands if c is not None]
        duration = max(cands) if cands else None

    fps = None
    if vstream:
        fps = parse_fps(vstream.get("avg_frame_rate")) or parse_fps(vstream.get("r_frame_rate"))

    tags = fmt.get("tags") or {}
    title = tags.get("title") or tags.get("TITLE") or path.stem

    return {
        "kind": "file",
        "title": title,
        "duration": round(duration, 3) if duration is not None else None,
        "width": vstream.get("width") if vstream else None,
        "height": vstream.get("height") if vstream else None,
        "fps": fps,
        "has_audio": astream is not None,
        "has_video": vstream is not None,
        # 设计决定：本地文件不提取内嵌字幕（transcribe.py 只接受外部 vtt/srt），
        # 因此本地文件 captions 恒为空，needs_transcribe 等价于 has_audio。
        "captions": [],
    }


# ---------------------------------------------------------------------------
# URL：yt_dlp 仅元数据
# ---------------------------------------------------------------------------
def probe_url(url: str) -> dict:
    try:
        import yt_dlp  # 延迟导入：未安装不影响其他脚本
    except ImportError:
        fail("缺少 yt-dlp：请先运行 python scripts/setup.py --install（或 pip install yt-dlp）")

    # 冻结契约指定的选项：只取元数据，不下载
    opts = {"quiet": True, "skip_download": True}
    log(f"yt_dlp 读取 URL 元数据: {url}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        fail(f"yt_dlp 元数据获取失败: {type(e).__name__}: {e}")
    if not info:
        fail("yt_dlp 未返回任何元数据")

    # 播放列表：探测第一个有效条目（整体元数据不代表单个视频）
    if info.get("entries") is not None:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            fail("播放列表为空，无法探测")
        log("检测到播放列表，探测第一个条目")
        info = entries[0]

    # 合并手动/自动字幕；同一语言手动优先（只记 manual）
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    captions = [{"lang": l, "kind": "manual"} for l in sorted(subs.keys(), key=lang_rank)]
    captions += [{"lang": l, "kind": "auto"}
                 for l in sorted(autos.keys(), key=lang_rank) if l not in subs]

    formats = info.get("formats") or []
    vcodec, acodec = info.get("vcodec"), info.get("acodec")
    if formats:
        has_video = any(f.get("vcodec") not in (None, "none") for f in formats)
        has_audio = any(f.get("acodec") not in (None, "none") for f in formats)
    else:
        # 无 formats 明细时看顶层 codec；信息缺失则保守按“有声有画”处理
        has_video = (vcodec not in (None, "none")) if vcodec is not None else True
        has_audio = (acodec not in (None, "none")) if acodec is not None else True

    width, height = info.get("width"), info.get("height")
    fps = to_float(info.get("fps"))
    if (width is None or height is None) and formats:
        # 退化：取 formats 里宽度最大的视频格式
        best = max((f for f in formats if f.get("width")),
                   key=lambda f: f.get("width") or 0, default=None)
        if best:
            width = width if width is not None else best.get("width")
            height = height if height is not None else best.get("height")
            fps = fps if fps is not None else to_float(best.get("fps"))

    duration = to_float(info.get("duration"))
    return {
        "kind": "url",
        "title": info.get("title") or url,
        "duration": round(duration, 3) if duration is not None else None,
        "width": width,
        "height": height,
        "fps": round(fps, 3) if fps is not None else None,
        "has_audio": has_audio,
        "has_video": has_video,
        "captions": captions,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="探测视频元数据：本地文件走 ffprobe，URL 用 yt_dlp 只取信息不下载。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--input", required=True,
                    help="视频 URL（http/https）或本地文件路径")
    args = ap.parse_args(argv)

    raw = str(args.input).strip()
    if is_url(raw):
        meta = probe_url(raw)
    elif Path(raw).exists():
        meta = probe_file(Path(raw))
    else:
        fail(f"输入既不是 http(s) URL 也不是存在的本地文件: {raw}")

    duration = meta["duration"]
    needs_transcribe = bool(meta["has_audio"]) and not meta["captions"]
    suggested_budget = frame_budget(duration) if duration else None  # 时长未知则无法给预算

    emit({
        "ok": True,
        "kind": meta["kind"],
        "title": meta["title"],
        "duration": meta["duration"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "has_audio": meta["has_audio"],
        "has_video": meta["has_video"],
        "captions": meta["captions"],
        "needs_transcribe": needs_transcribe,
        "suggested_budget": suggested_budget,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # 兜底：任何未预期异常都按契约输出 RESULT_JSON
        fail(f"未预期错误: {type(e).__name__}: {e}")
