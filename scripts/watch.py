#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py — video-watch skill 的总编排脚本（Orchestrator）。

流水线：probe →（URL 则 download）→（字幕优先，除非 --force-whisper）transcribe
        → frames → 写 manifest.json。

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
# 复用 common（契约约定同目录有 common.py）。导入失败时启用最小兜底实现，
# 行为与冻结契约一致，保证 watch.py 在 common 缺失时仍可独立运行。
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


def run_step(script: str, args: list, label: str, fatal: bool = True) -> dict | None:
    """
    调用同目录子脚本并解析其 RESULT_JSON。

    - subprocess 用 list 形式，不使用 shell=True（Windows 安全）。
    - 子进程 stdout/stderr 透传到本脚本日志（RESULT_JSON 行除外）。
    - fatal=True（默认）：找不到 RESULT_JSON、退出码非 0 或 ok=false 时立即中止整个流水线。
    - fatal=False：失败时记日志并返回 None，由调用方决定降级路径（如缓存模式的回退）。
    """
    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + [str(a) for a in args]
    log(f"▶ {label}")
    log(f"  $ python scripts/{script} {' '.join(str(a) for a in args)}")
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")  # 保证子进程输出 UTF-8
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        fail(f"{label} 启动失败: {exc}")

    tag = script[:-3] if script.endswith(".py") else script
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
                    fail(f"{label} 的 RESULT_JSON 解析失败: {exc}")
                return None
            break
    if result is None:
        if fatal:
            fail(f"{label} 未输出 RESULT_JSON（退出码 {proc.returncode}）")
        return None
    if proc.returncode != 0 or not result.get("ok"):
        if fatal:
            fail(f"{label} 失败: {result.get('error') or f'退出码 {proc.returncode}'}")
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
               "  python scripts/watch.py ./lecture.mp4 --start 12:30 --end 18:00 --width 1024\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="视频 URL 或本地文件路径")
    ap.add_argument("--start", default=None,
                    help="聚焦窗口起点：秒 / MM:SS / HH:MM:SS（作用于抽帧窗口）")
    ap.add_argument("--end", default=None,
                    help="聚焦窗口终点：秒 / MM:SS / HH:MM:SS（作用于抽帧窗口）")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="帧数硬上限，透传 frames.py（默认由 frames.py 自动决定）")
    ap.add_argument("--budget", default=None, metavar="auto|N",
                    help="帧预算：auto 或整数，透传 frames.py（默认 auto）")
    ap.add_argument("--width", type=int, default=512,
                    help="抽帧宽度像素，等比缩放（默认 512；屏幕文字多建议 1024）")
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
    ap.add_argument("--out-dir", default=None,
                    help="run 输出目录（默认 <skill>/runs/<slug>_<yyyymmdd_hhmmss>/）")
    return ap.parse_args()


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

    # ---- 变量初始化 --------------------------------------------------------
    kind = "file"
    title = None
    duration = None
    has_video = True
    has_audio = True
    video_path = None
    transcript_info = {"source": "none", "files": None}
    transcript_txt = None

    # ---- 0/4 B站客户端本地缓存：免下载，直接用缓存的纯音频/无音视频 --------
    cache_mode = Path(args.input).is_dir()
    if cache_mode:
        dir_name = Path(args.input).resolve().name or "bilibili-cache"
        if args.out_dir:
            run_dir = Path(args.out_dir).resolve()
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_dir = SKILL_ROOT / "runs" / f"{slugify(dir_name)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log(f"  run 目录: {run_dir}")
        pc = run_step("prepare_cache.py",
                      ["--input", args.input, "--out-dir", str(run_dir)],
                      "0/4 读取B站缓存 (prepare_cache)")
        kind = "cache"
        video_path = pc.get("video_path")
        audio_path = pc.get("audio_path")
        title = pc.get("title") or dir_name
        duration = pc.get("duration")
        has_video = bool(video_path)
        has_audio = bool(audio_path)
        if args.no_transcribe:
            log("3/4 转写：--no-transcribe，跳过")
        else:
            # 纯离线：直接用缓存里的纯音频本地转写
            if transcript_txt is None:
                if not has_audio:
                    log("3/4 转写：缓存中无纯音频文件，source=none")
                else:
                    log(f"3/4 转写：使用缓存纯音频 + {args.engine} (model={args.model})")
                    tr = run_step(
                        "transcribe.py",
                        ["--video", audio_path, "--out-dir", str(run_dir),
                         "--engine", args.engine, "--model", args.model,
                         "--language", args.language, "--device", args.device],
                        "3/4 缓存音频转写 (transcribe)",
                    )
                    engine_name = tr.get("engine") or args.engine
                    transcript_info = {
                        "source": engine_name,
                        "model": tr.get("model"),
                        "language": tr.get("language"),
                        "device": tr.get("device"),
                        "compute_type": tr.get("compute_type"),
                        "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
                    }
                    transcript_txt = tr.get("txt")

    if not cache_mode:
        # ---- 1/4 probe：探测输入 -------------------------------------------
        probe = run_step("probe.py", ["--input", args.input], "1/4 探测输入 (probe)")
        kind = probe.get("kind")                      # "url" | "file"
        title = probe.get("title") or Path(args.input).stem or "video"
        duration = probe.get("duration")
        has_video = probe.get("has_video", True)
        has_audio = probe.get("has_audio", True)
        log(f"  输入类型={kind} 标题={title!r} 时长={duration} 有视频={has_video} 有音频={has_audio}")

        # ---- 准备 run 目录 ---------------------------------------------------
        if args.out_dir:
            run_dir = Path(args.out_dir).resolve()
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_dir = SKILL_ROOT / "runs" / f"{slugify(title)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log(f"  run 目录: {run_dir}")

        # ---- 2/4 download：仅 URL 需要 --------------------------------------
        captions = []
        if kind == "url":
            dl_args = ["--url", args.input, "--out-dir", str(run_dir)]
            # 强制 whisper 或不转写时字幕无用，跳过字幕下载节省时间
            if args.force_whisper or args.no_transcribe:
                dl_args.append("--no-captions")
            dl = run_step("download.py", dl_args, "2/4 下载视频 (download)")
            video_path = dl.get("video_path")
            title = dl.get("title") or title
            duration = dl.get("duration") or duration
            captions = dl.get("captions") or []
        else:
            video_path = str(Path(args.input).resolve())  # 绝对路径，保证子进程任意 cwd 可用
            log("2/4 本地文件，跳过下载")

        # ---- 3/4 transcribe：字幕优先，除非 --force-whisper ------------------
        if args.no_transcribe:
            log("3/4 转写：--no-transcribe，跳过")
        else:
            cap = None if args.force_whisper else pick_caption(captions)
            if cap:
                log(f"3/4 转写：使用平台字幕 lang={cap.get('lang')} kind={cap.get('kind')}")
                tr = run_step(
                    "transcribe.py",
                    ["--vtt", cap["path"], "--out-dir", str(run_dir)],
                    "3/4 解析字幕 (transcribe --vtt)",
                )
                transcript_info = {
                    "source": "captions",
                    "lang": cap.get("lang"),
                    "kind": cap.get("kind"),
                    "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
                }
                transcript_txt = tr.get("txt")
            elif not has_audio:
                # 无音频流且无字幕可转，标记 none 而不是让 transcribe 报错
                log("3/4 转写：无音频流且无可用字幕，source=none")
            else:
                reason = "--force-whisper" if args.force_whisper else "无可用字幕"
                log(f"3/4 转写：{reason}，使用 {args.engine} (model={args.model}, lang={args.language})")
                tr = run_step(
                    "transcribe.py",
                    ["--video", video_path, "--out-dir", str(run_dir),
                     "--engine", args.engine, "--model", args.model,
                     "--language", args.language, "--device", args.device],
                    "3/4 语音转写 (transcribe)",
                )
                engine_name = tr.get("engine") or args.engine  # faster-whisper | sensevoice
                transcript_info = {
                    "source": engine_name,
                    "model": tr.get("model"),
                    "language": tr.get("language"),
                    "device": tr.get("device"),
                    "compute_type": tr.get("compute_type"),
                    "files": {"srt": tr.get("srt"), "txt": tr.get("txt"), "json": tr.get("json")},
                }
                transcript_txt = tr.get("txt")

    # ---- 4/4 frames：抽帧 ------------------------------------------------
    frames_info = {"count": 0, "dir": None}
    if args.no_frames:
        log("4/4 抽帧：--no-frames，跳过")
    elif not has_video:
        log("4/4 抽帧：输入无视频流（纯音频），自动跳过")
    else:
        fr_args = ["--video", video_path, "--out-dir", str(run_dir),
                   "--width", str(args.width)]
        if args.start is not None:
            fr_args += ["--start", str(args.start)]
        if args.end is not None:
            fr_args += ["--end", str(args.end)]
        if args.budget is not None:
            fr_args += ["--budget", str(args.budget)]
        if args.max_frames is not None:
            fr_args += ["--max-frames", str(args.max_frames)]
        fr = run_step("frames.py", fr_args, "4/4 抽帧 (frames)")
        frames_json = fr.get("frames_json")
        frames_dir = str(Path(frames_json).parent) if frames_json else str(run_dir / "frames")
        frames_info = {"count": fr.get("count", 0), "dir": frames_dir}

    # ---- manifest.json ----------------------------------------------------
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input": args.input,
        "title": title,
        "duration": duration,
        "window": {"start": start_s, "end": end_s},
        "video_path": video_path,
        "transcript": transcript_info,   # source: captions|faster-whisper|sensevoice|none
        "frames": frames_info,
        "params": {
            "engine": args.engine,
            "model": args.model,
            "language": args.language,
            "width": args.width,
            "budget": args.budget,
            "max_frames": args.max_frames,
            "no_frames": args.no_frames,
            "no_transcribe": args.no_transcribe,
            "force_whisper": args.force_whisper,
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"  manifest 已写入: {manifest_path}")

    # ---- 最终结果 ----------------------------------------------------------
    log("✅ 完成")
    result = {
        "ok": True,
        "run_dir": str(run_dir),
        "manifest": str(manifest_path),
        "transcript_txt": transcript_txt,
        "frames_dir": frames_info["dir"],
        "frame_count": frames_info["count"],
        "duration": duration,
        "title": title,
        "transcript_source": transcript_info.get("source"),
    }
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
