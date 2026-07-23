#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py — video-watch 总编排 CLI。

流水线：probe →（URL 则 download）→（字幕优先，除非 --force-whisper）transcribe
        → frames → 写 manifest.json。

多集编排：--item 支持 '3'（单集）/ '3-7'（区间）/ 'all'（全部），原样透传
        download.py；download 自身只下首集并在 RESULT 给 requested_items，
        多于 1 个时进入多集模式——逐集执行完整流水线（下载→转写→抽帧→review），
        每集独立 run 目录（runs/<标题>_pNN_<时间戳>/）；单集失败记 status 后
        继续下一集，不中断整体。多集模式 RESULT_JSON 聚合为
        {ok, episodes, succeeded, failed, total}，全部失败才 ok:false。
        --item all 且集数 >10 时打印安全闸警告（总集数/累计时长/预计下载量），
        但仍执行——是否确认继续由上层 AI 判断（SKILL.md 负责）。

通过 subprocess 依次调用同目录其他脚本并解析各自的 RESULT_JSON；
任一环节失败即中止：错误写 stderr、退出码 1、最后一行输出
RESULT_JSON: {"ok": false, "error": "..."}。

用法（任意 cwd 下）：
    python scripts/watch.py <URL或本地路径> [选项]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Windows 优先：自身 stdout/stderr 做 UTF-8 errors='replace' 包装
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent   # skill 根目录
SCRIPTS_DIR = Path(__file__).resolve().parent         # scripts/ 目录
RESULT_PREFIX = "RESULT_JSON: "

# ---------------------------------------------------------------------------
# 优先复用同目录 common.py；导入失败时启用最小兜底实现，保证脚本被
# 单独分发时仍可运行。
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, str(SCRIPTS_DIR))
    import common  # type: ignore

    slugify = common.slugify
    parse_time = common.parse_time
except Exception:  # pragma: no cover - 兜底分支

    def slugify(text, maxlen=40):
        """兜底 slug：保留中英文数字，其余折叠为 '-'，≤maxlen 字符。"""
        out = []
        for ch in str(text):
            if ch.isalnum():  # CJK 字符 isalnum() 为 True
                out.append(ch)
            elif ch in " -_":
                out.append("-")
        slug = "".join(out).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug[:maxlen] or "video"

    def parse_time(value):
        """兜底时间解析：秒(float) / 'MM:SS' / 'HH:MM:SS' → 秒(float)。"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if not s:
            return None
        if ":" in s:
            total = 0.0
            for part in s.split(":"):
                total = total * 60 + float(part)
            return total
        return float(s)


def log(msg: str) -> None:
    """进度日志：打在 stdout 的 RESULT_JSON 之前。"""
    print(f"[watch] {msg}", flush=True)


def fail(msg: str) -> None:
    """统一失败出口：stderr 说明 + 最后一行 RESULT_JSON + 退出码 1。"""
    print(f"[watch] ERROR: {msg}", file=sys.stderr, flush=True)
    print(RESULT_PREFIX + json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(1)


def _display_arg(value) -> str:
    """Return a log-safe CLI argument.

    Signed media URLs often carry short-lived credentials in their query string.
    The child process still receives the original argument; only logs/manifests use
    this redacted representation.
    """
    text = str(value)
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        return text
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        return urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except Exception:
        return "<redacted-url>"


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON without exposing a half-written manifest after interruption."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class EpisodeFailed(RuntimeError):
    """单集流水线失败：多集模式下记录该集 failed 后继续下一集，不中断整体。"""


# --item 选集表达式：'3'（单集）/ '3-7'（闭区间）/ 'all'（全部）
_ITEM_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

# 安全闸估算：720p（download.py 默认 --max-height 720）码率按 2 Mbps 估，
# 仅用于数量级提示，不代表真实下载量
_EST_BYTES_PER_SEC = 2_000_000 / 8
# --item all 的安全闸阈值：请求集数超过该值打印醒目警告（仍继续执行）
_ALL_ITEMS_WARN_THRESHOLD = 10


def parse_item_spec(spec) -> tuple:
    """--item 表达式 → (首集, 末集)；'all' → (1, None)（末集由 download 展开）。

    单集 '3' → (3, 3)；区间 '3-7' → (3, 7)。非法表达式抛 ValueError。
    只校验写法；集数范围由 download.py 按实际播放列表校验（以其为准）。
    """
    text = str(spec).strip().lower()
    if text == "all":
        return (1, None)
    m = _ITEM_RANGE_RE.fullmatch(text)
    if not m:
        raise ValueError(f"无法解析 {spec!r}（支持 '3' / '3-7' / 'all'）")
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) is not None else first
    if first < 1:
        raise ValueError(f"集数从 1 起: {spec!r}")
    if last < first:
        raise ValueError(f"区间终点小于起点: {spec!r}")
    return (first, last)


def _coerce_items(value) -> list:
    """download 返回的 requested_items → 去重保序的正整数列表；缺失/非法 → []。"""
    if not isinstance(value, list):
        return []
    items = []
    for v in value:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 1 and n not in items:
            items.append(n)
    return items


def _fmt_duration(seconds) -> str:
    """累计时长人类可读：'3 小时 25 分钟' / '45 分钟' / '50 秒'。"""
    total = max(0, int(round(float(seconds))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"
    if m:
        return f"{m} 分钟"
    return f"{s} 秒"


def _fmt_size(nbytes) -> str:
    """预计下载量人类可读：'12.3 GB' / '456 MB'。"""
    n = float(nbytes)
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    return f"{n / (1 << 20):.0f} MB"


def _warn_all_items(count, total_duration) -> None:
    """--item all 安全闸：集数 >10 时打印醒目警告（总集数/累计时长/预计下载量）。

    只警告、不阻断：是否继续的判断留给上层 AI（SKILL.md 负责确认流程）。
    """
    if not count or count <= _ALL_ITEMS_WARN_THRESHOLD:
        return
    log("=" * 64)
    log(f"⚠️  安全闸警告：--item all 将连续处理全部 {count} 集"
        f"（阈值 {_ALL_ITEMS_WARN_THRESHOLD} 集）")
    if total_duration:
        est = total_duration * _EST_BYTES_PER_SEC
        log(f"    累计时长约 {_fmt_duration(total_duration)}，"
            f"预计下载量约 {_fmt_size(est)}（按 720p≈2Mbps 估算）")
    else:
        log("    累计时长未知，预计下载量无法估算")
    log("    本脚本不阻断执行；是否继续请由上层 AI 与用户确认。")
    log("=" * 64)


def _step_abort(msg: str, raise_on_fail: bool) -> None:
    """子步骤致命失败出口：多集模式抛 EpisodeFailed（记单集失败），否则 fail() 退出。"""
    if raise_on_fail:
        print(f"[watch] ERROR: {msg}", file=sys.stderr, flush=True)
        raise EpisodeFailed(msg)
    fail(msg)


def run_step(script: str, args: list, label: str, fatal: bool = True,
             timeout: float | None = None, raise_on_fail: bool = False) -> dict | None:
    """
    调用同目录子脚本并解析其 RESULT_JSON。

    - subprocess 用 list 形式，不使用 shell=True（Windows 安全）。
    - 子进程 stdout/stderr 透传到本脚本日志（RESULT_JSON 行除外）。
    - fatal=True（默认）：找不到 RESULT_JSON、退出码非 0 或 ok=false 时立即中止整个流水线。
    - fatal=False：失败时记日志并返回 None，由调用方决定降级路径（如缓存模式的回退）。
    - raise_on_fail=True（多集模式）：致命失败改抛 EpisodeFailed，由编排循环记为单集失败。
    """
    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + [str(a) for a in args]
    log(f"▶ {label}")
    log(f"  $ python scripts/{script} {' '.join(_display_arg(a) for a in args)}")
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")  # 保证子进程输出 UTF-8
    # tag 需在 try 之前定义：TimeoutExpired 的 fatal=False 降级日志也会引用
    tag = script[:-3] if script.endswith(".py") else script
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timeout_desc = f"{timeout:g}s" if timeout else "配置时限"
        msg = f"{label} 超时（>{timeout_desc}）"
        if fatal:
            _step_abort(msg, raise_on_fail)
        log(f"  [{tag}] {msg}，降级处理")
        return None
    except OSError as exc:
        _step_abort(f"{label} 启动失败: {exc}", raise_on_fail)

    out = proc.stdout or ""
    for line in out.splitlines():
        if not line.startswith(RESULT_PREFIX):
            print(f"  [{tag}] {line}", flush=True)
    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            print(f"  [{tag}!] {line}", file=sys.stderr, flush=True)

    # 从末尾向前找 RESULT_JSON（契约保证它是最后一行，倒序查找最稳）
    result = None
    for line in reversed(out.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                result = json.loads(line[len(RESULT_PREFIX):])
            except json.JSONDecodeError as exc:
                if fatal:
                    _step_abort(f"{label} 的 RESULT_JSON 解析失败: {exc}", raise_on_fail)
                return None
            break
    if result is None:
        if fatal:
            _step_abort(f"{label} 未输出 RESULT_JSON（退出码 {proc.returncode}）", raise_on_fail)
        return None
    if proc.returncode != 0 or not result.get("ok"):
        if fatal:
            _step_abort(f"{label} 失败: {result.get('error') or f'退出码 {proc.returncode}'}", raise_on_fail)
        log(f"  [{script[:-3] if script.endswith('.py') else script}] 失败（非致命，降级处理）: "
            f"{result.get('error')}")
        return None
    return result


def pick_caption(captions: list) -> dict | None:
    """
    字幕选择优先级（与 download.py 契约一致）：手动 > 自动；语言 zh* > en* > 其他。
    """
    usable = [c for c in captions if c.get("path")]
    if not usable:
        return None

    def score(cap: dict):
        kind_score = 0 if cap.get("kind") == "manual" else 1
        lang = (cap.get("lang") or "").lower()
        if lang.startswith("zh"):
            lang_score = 0
        elif lang.startswith("en"):
            lang_score = 1
        else:
            lang_score = 2
        return (kind_score, lang_score)

    return sorted(usable, key=score)[0]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="video-watch 总编排：输入视频 URL 或本地路径，产出转写文本 + 抽帧图片 + manifest.json",
        epilog="示例:\n"
               "  python scripts/watch.py https://www.bilibili.com/video/BVxxxx\n"
               "  python scripts/watch.py ./meeting.mp4 --no-frames\n"
               "  python scripts/watch.py https://www.bilibili.com/video/BVxxxx --item 3-7\n"
               "  python scripts/watch.py ./lecture.mp4 --start 12:30 --end 18:00 --width 1024\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="视频 URL 或本地文件路径")
    ap.add_argument("--item", default=None, metavar="N|N-M|all",
                    help="多P/播放列表选集：'3'（单集）、'3-7'（闭区间）、'all'（全部），"
                         "透传 download.py；区间/全部时逐集独立 run 目录并聚合结果")
    ap.add_argument("--start", default=None,
                    help="聚焦窗口起点：秒 / MM:SS / HH:MM:SS（作用于转写与抽帧）")
    ap.add_argument("--end", default=None,
                    help="聚焦窗口终点：秒 / MM:SS / HH:MM:SS（作用于转写与抽帧）")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="帧数硬上限，透传 frames.py（默认由 frames.py 自动决定）")
    ap.add_argument("--budget", default=None, metavar="auto|N",
                    help="帧预算：auto 或整数，透传 frames.py（默认 auto）")
    ap.add_argument("--width", type=int, default=512,
                    help="抽帧宽度像素，等比缩放（默认 512；屏幕文字多建议 1024）")
    ap.add_argument("--mode", choices=["auto", "scene", "uniform"], default="auto",
                    help="首轮抽帧模式（默认 auto：均匀骨架 + 场景点）")
    ap.add_argument("--engine", default="faster-whisper",
                    choices=["faster-whisper", "sensevoice"],
                    help="无字幕时的语音转写引擎（默认 faster-whisper）")
    ap.add_argument("--model", default="small",
                    help="whisper 模型名：tiny/base/small/medium（默认 small）")
    ap.add_argument("--language", default="auto",
                    help="转写语言：auto/zh/en/...（默认 auto）")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                    help="转写设备：auto 自动（有 GPU 用 GPU）/cuda/cpu（默认 auto）")
    ap.add_argument("--no-frames", action="store_true",
                    help="跳过抽帧（播客/会议等纯音频内容，省 token）")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="跳过转写（只要画面时）")
    ap.add_argument("--force-whisper", action="store_true",
                    help="强制语音转写，即使有现成字幕也不用")
    ap.add_argument("--refine-plan", default=None,
                    help="可选 JSON 补帧计划；首轮抽帧后调用 refine.py 增量补帧")
    ap.add_argument("--refine-pass-id", default="r1",
                    help="补帧轮次标识（默认 r1）")
    ap.add_argument("--max-extra-frames", type=int, default=60,
                    help="本次补帧最多新增帧数（默认 60）")
    ap.add_argument("--no-review", action="store_true",
                    help="不生成按时间窗对齐的 review.json")
    ap.add_argument("--step-timeout", type=float, default=7200,
                    help="单个子步骤超时秒数；0 表示不限制（默认 7200）")
    ap.add_argument("--out-dir", default=None,
                    help="run 输出目录（默认 <skill>/runs/<slug>_<yyyymmdd_hhmmss>/）")
    return ap.parse_args()


def process_one(args: argparse.Namespace, ctx: dict) -> dict:
    """
    单个视频/单集的处理主体：转写 → 抽帧 → manifest →（可选补帧）→ review。

    ctx 必填键：step（run_step 闭包，失败语义由编排层决定）、run_dir、kind、
    title、duration、has_video、has_audio、video_path、audio_path、timeline、
    start_s、end_s；另需 captions（无字幕传 []）；raise_on_fail=True 时
    内部致命失败抛 EpisodeFailed（多集模式），否则走 fail() 统一出口。
    返回与单集模式 RESULT_JSON 同形的结果 dict。
    """
    step = ctx["step"]
    run_dir = ctx["run_dir"]
    kind = ctx["kind"]
    title = ctx["title"]
    duration = ctx["duration"]
    has_video = ctx["has_video"]
    has_audio = ctx["has_audio"]
    video_path = ctx["video_path"]
    audio_path = ctx["audio_path"]
    captions = ctx.get("captions") or []
    timeline = ctx["timeline"]
    start_s = ctx["start_s"]
    end_s = ctx["end_s"]

    def _abort(msg: str) -> None:
        # 与 run_step 的失败语义对齐：多集模式抛 EpisodeFailed，否则走统一失败出口
        if ctx.get("raise_on_fail"):
            print(f"[watch] ERROR: {msg}", file=sys.stderr, flush=True)
            raise EpisodeFailed(msg)
        fail(msg)

    def add_window(cli_args: list) -> list:
        if args.start is not None:
            cli_args += ["--start", str(args.start)]
        if args.end is not None:
            cli_args += ["--end", str(args.end)]
        return cli_args

    # ---- 3/4 transcribe：字幕优先，除非 --force-whisper ------------------
    transcript_info = {"source": "none", "files": None}
    transcript_txt = None
    if args.no_transcribe:
        log("3/4 转写：--no-transcribe，跳过")
    elif kind == "cache":
        # 纯离线：直接用缓存里的纯音频本地转写
        if not has_audio:
            log("3/4 转写：缓存中无纯音频文件，source=none")
        else:
            log(f"3/4 转写：使用缓存纯音频 + {args.engine} (model={args.model})")
            audio_source_offset = timeline.get("audio_minus_video_start")
            tr_args = add_window(
                ["--audio", audio_path, "--out-dir", str(run_dir),
                 "--engine", args.engine, "--model", args.model,
                 "--language", args.language, "--device", args.device]
            )
            if isinstance(audio_source_offset, (int, float)) and not isinstance(
                audio_source_offset, bool
            ):
                tr_args += ["--source-offset", str(audio_source_offset)]
            tr = step(
                "transcribe.py", tr_args,
                "3/4 缓存音频转写 (transcribe)",
            )
            engine_name = tr.get("engine") or args.engine
            transcript_info = {
                "source": engine_name,
                "model": tr.get("model"),
                "language": tr.get("language"),
                "device": tr.get("device"),
                "compute_type": tr.get("compute_type"),
                "segments": tr.get("segments"),
                "window": tr.get("window"),
                "timeline": tr.get("timeline"),
                "audio": tr.get("audio"),
                "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
            }
            transcript_txt = tr.get("txt")
    else:
        cap = None if args.force_whisper else pick_caption(captions)
        if cap:
            log(f"3/4 转写：使用平台字幕 lang={cap.get('lang')} kind={cap.get('kind')}")
            tr_args = add_window(["--vtt", cap["path"], "--out-dir", str(run_dir)])
            tr = step(
                "transcribe.py", tr_args,
                "3/4 解析字幕 (transcribe --vtt)",
            )
            transcript_info = {
                "source": "captions",
                "lang": cap.get("lang"),
                "kind": cap.get("kind"),
                "segments": tr.get("segments"),
                "window": tr.get("window"),
                "timeline": tr.get("timeline"),
                "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
            }
            transcript_txt = tr.get("txt")
        elif not has_audio:
            # 无音频流且无字幕可转，标记 none 而不是让 transcribe 报错
            log("3/4 转写：无音频流且无可用字幕，source=none")
        else:
            reason = "--force-whisper" if args.force_whisper else "无可用字幕"
            log(f"3/4 转写：{reason}，使用 {args.engine} (model={args.model}, lang={args.language})")
            tr_args = add_window(
                ["--video", video_path, "--out-dir", str(run_dir),
                 "--engine", args.engine, "--model", args.model,
                 "--language", args.language, "--device", args.device]
            )
            tr = step(
                "transcribe.py", tr_args,
                "3/4 语音转写 (transcribe)",
            )
            engine_name = tr.get("engine") or args.engine  # faster-whisper | sensevoice
            transcript_info = {
                "source": engine_name,
                "model": tr.get("model"),
                "language": tr.get("language"),
                "device": tr.get("device"),
                "compute_type": tr.get("compute_type"),
                "segments": tr.get("segments"),
                "window": tr.get("window"),
                "timeline": tr.get("timeline"),
                "audio": tr.get("audio"),
                "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
            }
            transcript_txt = tr.get("txt")

    # ---- 4/4 frames：抽帧 ------------------------------------------------
    frames_info = {
        "count": 0,
        "base_count": 0,
        "dir": None,
        "json": None,
        "passes": [],
    }
    if args.no_frames:
        log("4/4 抽帧：--no-frames，跳过")
    elif not has_video:
        log("4/4 抽帧：输入无视频流（纯音频），自动跳过")
    else:
        fr_args = ["--video", video_path, "--out-dir", str(run_dir),
                   "--width", str(args.width), "--mode", args.mode,
                   "--pass-id", "base"]
        if args.start is not None:
            fr_args += ["--start", str(args.start)]
        if args.end is not None:
            fr_args += ["--end", str(args.end)]
        if args.budget is not None:
            fr_args += ["--budget", str(args.budget)]
        if args.max_frames is not None:
            fr_args += ["--max-frames", str(args.max_frames)]
        fr = step("frames.py", fr_args, "4/4 抽帧 (frames)")
        frames_json = fr.get("frames_json")
        frames_dir = str(Path(frames_json).parent) if frames_json else str(run_dir / "frames")
        base_count = fr.get("count", 0)
        frames_info = {
            "count": base_count,
            "base_count": base_count,
            "dir": frames_dir,
            "json": frames_json,
            "passes": [{
                "pass_id": fr.get("pass_id") or "base",
                "count": base_count,
                "window": fr.get("window"),
                "mode": args.mode,
            }],
        }

    # ---- manifest.json 首次落盘：转写+首轮抽帧已齐，先保底，review/refine 后再更新 ----
    review_info = {"status": "pending", "json": None}
    manifest = {
        "schema_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input": _display_arg(args.input),
        "source_kind": kind,
        "title": title,
        "duration": duration,
        "window": {"start": start_s, "end": end_s},
        "video_path": video_path,
        "audio_path": audio_path,
        "timeline": timeline,
        "transcript": transcript_info,   # source: captions|faster-whisper|sensevoice|none
        "frames": frames_info,
        "review": review_info,
        "params": {
            "engine": args.engine,
            "model": args.model,
            "language": args.language,
            "width": args.width,
            "mode": args.mode,
            "budget": args.budget,
            "max_frames": args.max_frames,
            "no_frames": args.no_frames,
            "no_transcribe": args.no_transcribe,
            "force_whisper": args.force_whisper,
            "step_timeout": args.step_timeout,
            "refine_plan": str(Path(args.refine_plan).resolve()) if args.refine_plan else None,
            "refine_pass_id": args.refine_pass_id if args.refine_plan else None,
            "max_extra_frames": args.max_extra_frames,
            "no_review": args.no_review,
        },
    }
    manifest_path = run_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)
    log(f"  manifest 已写入: {manifest_path}")

    # ---- 可选 refinement：消费外部/Agent 生成的通用 JSON 计划 ---------------
    if args.refine_plan:
        if not has_video or not video_path:
            _abort("--refine-plan 无法用于纯音频输入")
        refine_args = [
            "--video", video_path,
            "--out-dir", str(run_dir),
            "--plan", str(Path(args.refine_plan).resolve()),
            "--pass-id", args.refine_pass_id,
            "--width", str(args.width),
            "--max-extra", str(args.max_extra_frames),
        ]
        # 补帧失败不致命：首轮 frames 与 manifest 已保底，记失败轮次后继续
        refined = step("refine.py", refine_args, "补充抽帧 (refine)", fatal=False)
        if refined is None:
            frames_info["passes"].append({
                "pass_id": args.refine_pass_id,
                "count": 0,
                "status": "failed",
                "plan": str(Path(args.refine_plan).resolve()),
            })
        else:
            added = refined.get("added_count", refined.get("count", 0))
            frames_info["count"] = refined.get(
                "total_count", frames_info["count"] + added
            )
            frames_info["json"] = refined.get("frames_json") or frames_info["json"]
            if frames_info["json"]:
                frames_info["dir"] = str(Path(frames_info["json"]).parent)
            frames_info["passes"].append({
                "pass_id": refined.get("pass_id") or args.refine_pass_id,
                "count": added,
                "plan": str(Path(args.refine_plan).resolve()),
                "warnings": refined.get("warnings") or [],
            })

    # ---- review packet：供任意人类/多模态模型结构化判断（失败降级，不摧毁 run） ----
    transcript_json = (transcript_info.get("files") or {}).get("json")
    if args.no_review:
        review_info["status"] = "skipped"
        review_info["reason"] = "--no-review"
    elif not frames_info.get("json"):
        review_info["status"] = "skipped"
        review_info["reason"] = "no_frames"
    else:
        review_path = run_dir / "review.json"
        review_args = [
            "prepare",
            "--frames-json", frames_info["json"],
            "--out", str(review_path),
        ]
        if transcript_json:
            review_args += ["--transcript-json", transcript_json]
        if duration is not None:
            review_args += ["--duration", str(duration)]
        if start_s is not None:
            review_args += ["--start", str(start_s)]
        if end_s is not None:
            review_args += ["--end", str(end_s)]
        review_result = step("review.py", review_args, "生成视听审查包 (review)", fatal=False)
        if review_result is None:
            # review 失败只记状态：转写/抽帧成果与 manifest 均已保底
            review_info["status"] = "failed"
            review_info["reason"] = "review 子步骤失败（详见日志）"
        else:
            review_info.update({
                "status": (
                    "pending_assessment"
                    if transcript_json
                    else "pending_visual_review"
                ),
                "json": review_result.get("review_json") or str(review_path),
                "units": review_result.get("units"),
            })

    # ---- review/refine 结果已定，更新 manifest 并再次落盘 ------------------
    manifest["review"] = review_info
    _write_json_atomic(manifest_path, manifest)

    # ---- 最终结果 ----------------------------------------------------------
    log("✅ 完成")
    return {
        "ok": True,
        "run_dir": str(run_dir),
        "manifest": str(manifest_path),
        "transcript_txt": transcript_txt,
        "frames_dir": frames_info["dir"],
        "frames_json": frames_info["json"],
        "frame_count": frames_info["count"],
        "frame_passes": frames_info["passes"],
        "review_json": review_info.get("json"),
        "duration": duration,
        "title": title,
        "transcript_source": transcript_info.get("source"),
    }


def _episode_run_dir(args: argparse.Namespace, title: str, item: int) -> Path:
    """单集 run 目录：默认 runs/<标题slug>_pNN_<时间戳>/；--out-dir 时为其 pNN 子目录。"""
    if args.out_dir:
        return Path(args.out_dir).resolve() / f"p{item:02d}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return SKILL_ROOT / "runs" / f"{slugify(title)}_p{item:02d}_{stamp}"


def run_multi_episodes(args: argparse.Namespace, base: dict, dl_first: dict,
                       requested_items: list) -> None:
    """
    多集编排：对 requested_items 逐集执行完整流水线（下载→转写→抽帧→review）。

    - 首集复用 main 已完成的下载产物与 run 目录；其余各集独立 run 目录，
      逐集调用 download.py --item N；
    - 单集失败记 episodes[].ok=false（含 error）后继续，不中断整体；
    - 聚合 RESULT_JSON：{ok, episodes, succeeded, failed, total}；
      全部失败才 ok:false（并遵守错误契约：stderr + 退出码 1）。
    """
    step_timeout = None if args.step_timeout == 0 else args.step_timeout

    def ep_step(script: str, cli_args: list, label: str, fatal: bool = True):
        # 多集模式：致命失败改抛 EpisodeFailed，由下方循环捕获记为单集失败
        return run_step(script, cli_args, label, fatal=fatal,
                        timeout=step_timeout, raise_on_fail=True)

    total = len(requested_items)
    preview = ", ".join(str(n) for n in requested_items[:10])
    if total > 10:
        preview += ", …"
    log(f"═══ 多集模式：共 {total} 集（{preview}）═══")

    episodes = []
    succeeded = 0
    for seq, item in enumerate(requested_items, 1):
        log(f"── 第 {item} 集（{seq}/{total}）" + "─" * 30)
        if seq == 1:
            # 首集：下载已在 main 中完成，复用其 run 目录与媒体产物
            run_dir = base["run_dir"]
            dl = dl_first
        else:
            run_dir = _episode_run_dir(args, base["title"], item)
            run_dir.mkdir(parents=True, exist_ok=True)
            log(f"  run 目录: {run_dir}")
            dl = None
        record = {"item": item, "ok": False, "run_dir": str(run_dir),
                  "transcript_txt": None, "frames_json": None, "review_json": None}
        try:
            if dl is None:
                dl_args = ["--url", args.input, "--out-dir", str(run_dir),
                           "--item", str(item)]
                # 强制 whisper 或不转写时字幕无用，跳过字幕下载节省时间
                if args.force_whisper or args.no_transcribe:
                    dl_args.append("--no-captions")
                dl = ep_step("download.py", dl_args, f"2/4 下载第 {item} 集 (download)")
            # has_video/has_audio 取自 probe（同一播放列表各集通常一致）
            video_path = dl.get("video_path")
            result = process_one(args, {
                "step": ep_step,
                "raise_on_fail": True,
                "run_dir": run_dir,
                "kind": "url",
                "title": dl.get("title") or base["title"],
                "duration": dl.get("duration") or base["duration"],
                "has_video": base["has_video"],
                "has_audio": base["has_audio"],
                "video_path": video_path,
                "audio_path": video_path,
                "captions": dl.get("captions") or [],
                "timeline": {"origin": 0.0, "video_start": 0.0, "audio_start": 0.0},
                "start_s": base["start_s"],
                "end_s": base["end_s"],
            })
            record.update({
                "ok": True,
                "transcript_txt": result.get("transcript_txt"),
                "frames_json": result.get("frames_json"),
                "review_json": result.get("review_json"),
            })
            succeeded += 1
            log(f"  ✓ 第 {item} 集完成")
        except EpisodeFailed as exc:
            record["error"] = str(exc)
            log(f"  ✗ 第 {item} 集失败: {exc}（继续下一集）")
        episodes.append(record)

    failed = total - succeeded
    aggregate = {
        "ok": succeeded > 0,
        "episodes": episodes,
        "succeeded": succeeded,
        "failed": failed,
        "total": total,
    }
    if not aggregate["ok"]:
        # 全部失败才整体失败：遵守统一错误契约（stderr + 退出码 1 + ok:false）
        print(f"[watch] ERROR: 多集处理全部失败（共 {total} 集）", file=sys.stderr, flush=True)
        print(RESULT_PREFIX + json.dumps(aggregate, ensure_ascii=False))
        sys.exit(1)
    log(f"═══ 多集完成：成功 {succeeded} / 失败 {failed} / 共 {total} ═══")
    print(RESULT_PREFIX + json.dumps(aggregate, ensure_ascii=False))


def main() -> None:
    args = parse_args()

    # ---- 参数校验：时间窗口与帧预算 -------------------------------------
    try:
        start_s = parse_time(args.start) if args.start is not None else None
        end_s = parse_time(args.end) if args.end is not None else None
    except Exception as exc:
        fail(f"时间参数无效（--start/--end 接受 秒 / MM:SS / HH:MM:SS）: {exc}")
    if start_s is not None and end_s is not None and end_s <= start_s:
        fail(f"--end ({args.end}) 必须大于 --start ({args.start})")
    if args.budget is not None and args.budget != "auto":
        try:
            int(args.budget)
        except ValueError:
            fail("--budget 必须是 auto 或整数")
    if args.step_timeout < 0:
        fail("--step-timeout 不能为负数")
    if args.max_extra_frames < 1:
        fail("--max-extra-frames 必须 ≥ 1")
    if args.refine_plan and (args.no_frames or args.max_frames == 0):
        fail("--refine-plan 需要启用首轮抽帧")
    if args.refine_plan and not Path(args.refine_plan).is_file():
        fail(f"补帧计划不存在: {Path(args.refine_plan).resolve()}")

    # --item 选集表达式：'3' / '3-7' / 'all'；'all' 时 item_last=None（末集由 download 展开）
    item_first = item_last = None
    if args.item is not None:
        try:
            item_first, item_last = parse_item_spec(args.item)
        except ValueError as exc:
            fail(f"--item 表达式无效: {exc}")
    item_all = args.item is not None and item_last is None

    step_timeout = None if args.step_timeout == 0 else args.step_timeout

    def step(script: str, cli_args: list, label: str, fatal: bool = True):
        return run_step(script, cli_args, label, fatal=fatal, timeout=step_timeout)


    # ---- 变量初始化 --------------------------------------------------------
    kind = "file"
    title = None
    duration = None
    has_video = True
    has_audio = True
    video_path = None
    audio_path = None
    timeline = {"origin": 0.0, "video_start": 0.0, "audio_start": 0.0}
    transcript_info = {"source": "none", "files": None}
    transcript_txt = None

    # ---- 0/4 B站客户端本地缓存：免下载，直接用缓存的纯音频/无音视频 --------
    input_candidate = Path(args.input)
    cache_mode = input_candidate.is_dir()
    if cache_mode and next(input_candidate.rglob("*.m4s"), None) is None:
        fail(f"输入是目录，但其中没有 B站 .m4s 缓存文件: {input_candidate.resolve()}")
    if cache_mode:
        if args.item is not None:
            log("  --item 仅对视频 URL 有效，缓存模式忽略该参数")
        dir_name = Path(args.input).resolve().name or "bilibili-cache"
        if args.out_dir:
            run_dir = Path(args.out_dir).resolve()
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run_dir = SKILL_ROOT / "runs" / f"{slugify(dir_name)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log(f"  run 目录: {run_dir}")
        pc = step("prepare_cache.py",
                  ["--input", args.input, "--out-dir", str(run_dir)],
                  "0/4 读取B站缓存 (prepare_cache)")
        timeline.update(pc.get("timeline") or {})
        ctx = {
            "step": step,
            "run_dir": run_dir,
            "kind": "cache",
            "title": pc.get("title") or dir_name,
            "duration": pc.get("duration"),
            "has_video": bool(pc.get("video_path")),
            "has_audio": bool(pc.get("audio_path")),
            "video_path": pc.get("video_path"),
            "audio_path": pc.get("audio_path"),
            "captions": [],
            "timeline": timeline,
            "start_s": start_s,
            "end_s": end_s,
        }
        result = process_one(args, ctx)
        print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False))
        return

    # ---- 1/4 probe：探测输入 -------------------------------------------
    probe = step("probe.py", ["--input", args.input], "1/4 探测输入 (probe)")
    kind = probe.get("kind")                      # "url" | "file"
    title = probe.get("title") or Path(args.input).stem or "video"
    duration = probe.get("duration")
    has_video = probe.get("has_video", True)
    has_audio = probe.get("has_audio", True)
    log(f"  输入类型={kind} 标题={title!r} 时长={duration} 有视频={has_video} 有音频={has_audio}")

    if kind != "url":
        # ---- 本地文件：--item 仅对 URL 有意义 ---------------------------
        if args.item is not None:
            log("  --item 仅对视频 URL 有效，本地文件忽略该参数")
        if args.out_dir:
            run_dir = Path(args.out_dir).resolve()
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run_dir = SKILL_ROOT / "runs" / f"{slugify(title)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log(f"  run 目录: {run_dir}")
        log("2/4 本地文件，跳过下载")
        video_path = str(Path(args.input).resolve())  # 绝对路径，保证子进程任意 cwd 可用
        result = process_one(args, {
            "step": step,
            "run_dir": run_dir,
            "kind": kind,
            "title": title,
            "duration": duration,
            "has_video": has_video,
            "has_audio": has_audio,
            "video_path": video_path,
            "audio_path": video_path,
            "captions": [],
            "timeline": timeline,
            "start_s": start_s,
            "end_s": end_s,
        })
        print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False))
        return

    # ---- URL：--item all 安全闸（probe 给出 playlist 清单时提前警告） -----
    gate_warned = False
    if item_all:
        playlist = probe.get("playlist") or {}
        pl_items = playlist.get("items") or []
        pl_count = playlist.get("count") or len(pl_items)
        pl_durations = [it.get("duration") for it in pl_items if isinstance(it, dict)]
        pl_total = sum(d for d in pl_durations if isinstance(d, (int, float)))
        if pl_count and pl_count > _ALL_ITEMS_WARN_THRESHOLD:
            _warn_all_items(pl_count, pl_total or None)
            gate_warned = True

    # ---- run 目录：--item 时首集即按多集命名（pNN 后缀） -------------------
    if args.item is not None:
        run_dir = _episode_run_dir(args, title, item_first)
    elif args.out_dir:
        run_dir = Path(args.out_dir).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_dir = SKILL_ROOT / "runs" / f"{slugify(title)}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"  run 目录: {run_dir}")

    # ---- 2/4 download：--item 原样透传；区间/全部时 download 只下首集 ------
    dl_args = ["--url", args.input, "--out-dir", str(run_dir)]
    # 强制 whisper 或不转写时字幕无用，跳过字幕下载节省时间
    if args.force_whisper or args.no_transcribe:
        dl_args.append("--no-captions")
    if args.item is not None:
        dl_args += ["--item", args.item]
    dl = step("download.py", dl_args, "2/4 下载视频 (download)")
    requested_items = _coerce_items(dl.get("requested_items"))

    # 安全闸兜底：probe 未给 playlist 清单时，按 download 展开的集数补警告
    if item_all and not gate_warned and requested_items:
        first_dur = dl.get("duration")
        _warn_all_items(len(requested_items),
                        first_dur * len(requested_items) if first_dur else None)

    if len(requested_items) > 1:
        # ---- 多集编排：逐集完整流水线，单集失败不中断，聚合输出 -------------
        run_multi_episodes(args, {
            "run_dir": run_dir,
            "title": title,
            "duration": duration,
            "has_video": has_video,
            "has_audio": has_audio,
            "start_s": start_s,
            "end_s": end_s,
        }, dl, requested_items)
        return

    # ---- 单集：默认流程 ----------------------------------------------------
    video_path = dl.get("video_path")
    result = process_one(args, {
        "step": step,
        "run_dir": run_dir,
        "kind": "url",
        "title": dl.get("title") or title,
        "duration": dl.get("duration") or duration,
        "has_video": has_video,
        "has_audio": has_audio,
        "video_path": video_path,
        "audio_path": video_path,
        "captions": dl.get("captions") or [],
        "timeline": timeline,
        "start_s": start_s,
        "end_s": end_s,
    })
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
