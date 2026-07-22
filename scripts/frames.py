#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frames.py — video-watch skill 抽帧脚本（冻结契约实现）

三种模式（--mode）：
  scene   仅用 ffmpeg 场景检测：-vf "select='gt(scene,0.3)',showinfo"，解析 showinfo 的 pts_time
  uniform 在 [start, end] 窗口均匀取 N 个“段中心”时间点
  auto    场景点优先；均匀网格点按 >=0.5s 最小间隔补齐到预算 N（默认）

预算 N：--budget auto 走 common.frame_budget；再受全局硬上限 2fps / 100 帧约束，
--fps / --max-frames 只能在此基础上继续收紧。

产物：<out-dir>/frames/{idx:04d}_t{秒:08.1f}.jpg + <out-dir>/frames/frames.json
stdout 最后一行：RESULT_JSON: {"ok": true, "count": N, "frames_json": ..., "window": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent  # skill 根目录

# ---------------------------------------------------------------------------
# 复用 common（契约约定同目录有 common.py）。导入失败时启用最小兜底实现，
# 行为与冻结契约一致，保证 frames.py 在 common 缺失时仍可独立运行。
# ---------------------------------------------------------------------------
try:
    import common  # type: ignore

    find_tool = common.find_tool
    parse_time = common.parse_time
    frame_budget = common.frame_budget
    fmt_ts = common.fmt_ts
except Exception:  # pragma: no cover - 兜底实现，行为严格按冻结契约

    def find_tool(name):
        """工具查找：<SKILL>/tools/<name>.exe → PATH。"""
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        local = SKILL_ROOT / "tools" / exe
        if local.is_file():
            return str(local)
        return shutil.which(name) or shutil.which(exe)

    def parse_time(v):
        """时间解析：秒(float) / "MM:SS" / "HH:MM:SS"。"""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        try:
            return float(s)
        except ValueError:
            pass
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ValueError(f"无法解析时间: {v!r}")
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        raise ValueError(f"无法解析时间: {v!r}")

    def frame_budget(duration, start=None, end=None):
        """帧预算：整片按时长分档；给了 start/end 的聚焦窗口按 1fps 密度、[20,100]。"""
        if start is not None or end is not None:
            s = 0.0 if start is None else float(start)
            e = float(duration) if end is None else float(end)
            return max(20, min(100, int(round(max(0.0, e - s) * 1.0))))
        d = float(duration)
        if d <= 30:
            return 30
        if d <= 60:
            return 40
        if d <= 180:
            return 60
        if d <= 600:
            return 80
        return 100

    def fmt_ts(sec):
        """时间戳显示：MM:SS，≥1 小时用 HH:MM:SS。"""
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(round(sec % 60))
        if s == 60:
            s, m = 0, m + 1
        if m == 60:
            m, h = 0, h + 1
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# 契约固定常量
MIN_GAP = 0.5       # 相邻帧最小间隔（秒）
SCENE_TH = 0.3      # 场景检测阈值 gt(scene,0.3)
HARD_FPS = 2.0      # 全局帧率硬上限
HARD_FRAMES = 100   # 全局帧数硬上限


# ---------------------------------------------------------------------------
# 输出与错误处理（契约：日志在前，stdout 最后一行单行 RESULT_JSON）
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[frames] {msg}", flush=True)


def emit_result(obj):
    print("RESULT_JSON: " + json.dumps(obj, ensure_ascii=False), flush=True)


def fail(msg, code=1):
    print(f"[frames][ERROR] {msg}", file=sys.stderr, flush=True)
    emit_result({"ok": False, "error": str(msg)})
    sys.exit(code)


class CliParser(argparse.ArgumentParser):
    """参数错误也遵守 RESULT_JSON 契约，便于 watch.py 编排消费。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        fail(f"参数错误: {message}")


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def probe_duration(ffprobe, video):
    """用 ffprobe 拿总时长（format 优先，video stream 兜底）。"""
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=duration",
        "-of", "json", video,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        fail(f"ffprobe 失败: {(r.stderr or '').strip()[:300]}")

    def _f(v):
        try:
            x = float(v)
            return x if x == x and x >= 0 else None  # 过滤 N/A / NaN
        except (TypeError, ValueError):
            return None

    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    dur = _f((data.get("format") or {}).get("duration"))
    if dur is None:
        for st in data.get("streams") or []:
            dur = _f(st.get("duration"))
            if dur is not None:
                break
    if dur is None:
        fail("无法获取视频时长（文件可能损坏或不含有效视频流）")
    return dur


def detect_scene_times(ffmpeg, video, start, end):
    """场景检测：select='gt(scene,0.3)',showinfo → 解析 pts_time，返回窗口内切换时刻（已去重排序）。

    -ss 置于 -i 之前：ffmpeg 从最近关键帧解码，输出时间轴以 start 为零点，
    因此实际时刻 = start + pts_time。
    """
    null_dev = "NUL" if os.name == "nt" else "/dev/null"
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video,
        "-vf", f"select='gt(scene,{SCENE_TH})',showinfo",
        "-an", "-sn", "-f", "null", null_dev,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        fail(f"场景检测失败: {(r.stderr or '').strip()[-300:]}")
    text = (r.stderr or "") + (r.stdout or "")
    pts = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", text)]
    times = sorted(start + p for p in pts)
    times = [t for t in times if start - 1e-6 <= t <= end + 1e-6]
    return dedupe_gap(times, MIN_GAP)


def dedupe_gap(pts, gap):
    """有序时间点去重：相邻间隔 < gap 时保留先者。"""
    out = []
    for t in sorted(pts):
        if not out or t - out[-1] >= gap - 1e-9:
            out.append(t)
    return out


def uniform_points(start, end, n):
    """[start,end] 内 n 个均匀“段中心”点（避开窗口端点，防止 EOF 处取不到帧）。"""
    step = (end - start) / n
    return [start + (i + 0.5) * step for i in range(n)]


def even_subsample(pts, n):
    """有序列表均匀抽稀到 n 个（保留首尾）。"""
    m = len(pts)
    if m <= n:
        return pts
    if n == 1:
        return [pts[m // 2]]
    return [pts[round(i * (m - 1) / (n - 1))] for i in range(n)]


def plan_points(mode, scene_pts, start, end, n):
    """按模式产出 [(t, source), ...]，source ∈ {"scene","uniform"}，按时间升序。"""
    if mode == "uniform":
        return [(t, "uniform") for t in uniform_points(start, end, n)]

    if mode == "scene":
        if not scene_pts:
            log("未检测到场景切换，scene 模式退化为均匀抽帧")
            return [(t, "uniform") for t in uniform_points(start, end, n)]
        pts = even_subsample(scene_pts, n) if len(scene_pts) > n else scene_pts
        return [(t, "scene") for t in pts]

    # auto：场景点优先，均匀网格按最小间隔补齐到 N
    kept = [(t, "scene") for t in (even_subsample(scene_pts, n) if len(scene_pts) > n else scene_pts)]

    def gap_ok(t):
        return all(abs(t - k[0]) >= MIN_GAP - 1e-9 for k in kept)

    for grid_n in (n, 4 * n):  # 第一轮段中心；补不满再用细网格兜底
        if len(kept) >= n:
            break
        for t in uniform_points(start, end, grid_n):
            if len(kept) >= n:
                break
            if gap_ok(t):
                kept.append((t, "uniform"))
    kept.sort(key=lambda x: x[0])
    return kept


def make_name(idx, t_disp):
    """契约命名：{idx:04d}_t{秒:08.1f}.jpg"""
    return f"{idx:04d}_t{t_disp:08.1f}.jpg"


def extract_one(ffmpeg, video, t, width, out_path):
    """逐时间点精确抽帧：-ss 前置（关键帧定位+精确解码），失败回退 t-0.6s 重试一次。

    返回 (ok, err)。scale 用 -2（等比且保证偶数尺寸，mjpeg/yuv420p 要求）。
    """
    attempts = [t]
    if t - 0.6 > 0:
        attempts.append(t - 0.6)
    last_err = ""
    for ts in attempts:
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{ts:.3f}", "-i", video, "-frames:v", "1", "-an",
        ]
        if width and width > 0:
            cmd += ["-vf", f"scale={width}:-2"]
        cmd += ["-q:v", "3", "-y", str(out_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            last_err = "超时(180s)"
            continue
        if r.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return True, ""
        err_lines = (r.stderr or "").strip().splitlines()
        last_err = err_lines[-1] if err_lines else f"exit={r.returncode}"
    return False, last_err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = CliParser(
        prog="frames.py",
        description="video-watch：从视频抽取代表性帧（auto=场景检测+均匀网格融合）",
    )
    p.add_argument("--video", required=True, help="输入视频文件路径（本地文件）")
    p.add_argument("--out-dir", required=True, help="输出目录；帧写入 <out-dir>/frames/")
    p.add_argument("--start", default=None, help="起始时间：秒 / MM:SS / HH:MM:SS（默认片头）")
    p.add_argument("--end", default=None, help="结束时间：秒 / MM:SS / HH:MM:SS（默认片尾）")
    p.add_argument("--budget", default="auto", help="帧数预算：auto（按契约自动，默认）或正整数 N")
    p.add_argument("--max-frames", type=int, default=None, help="帧数收紧上限（只能比全局 100 更严）")
    p.add_argument("--fps", type=float, default=None, help="帧率收紧上限（只能比全局 2fps 更严）")
    p.add_argument("--width", type=int, default=512, help="输出帧宽度 px（默认 512，高度等比；≤0 不缩放）")
    p.add_argument("--mode", choices=["auto", "scene", "uniform"], default="auto",
                   help="抽帧模式：auto（默认，融合）/ scene（纯场景检测）/ uniform（纯均匀网格）")
    return p


def main():
    # Windows 下 stdout/stderr 统一 UTF-8（errors=replace），契约要求
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args()

    try:
        # ---- 输入校验 ----
        video_path = Path(args.video).expanduser()
        if not video_path.is_file():
            fail(f"视频文件不存在: {args.video}")
        video = str(video_path.resolve())

        ffmpeg = find_tool("ffmpeg")
        ffprobe = find_tool("ffprobe")
        if not ffmpeg or not ffprobe:
            fail("未找到 ffmpeg/ffprobe，请先运行: python scripts/setup.py --install")
        log(f"ffmpeg = {ffmpeg}")
        log(f"ffprobe = {ffprobe}")

        # ---- 窗口 ----
        duration = probe_duration(ffprobe, video)
        user_start = parse_time(args.start) if args.start is not None else None
        user_end = parse_time(args.end) if args.end is not None else None
        start = 0.0 if user_start is None else user_start
        end = duration if user_end is None else user_end
        if start < 0:
            fail(f"--start 不能为负: {start}")
        if start >= duration:
            fail(f"--start ({start:.1f}s) 超出视频时长 ({duration:.1f}s)")
        if end > duration:
            log(f"警告: --end ({end:.1f}s) 超出时长，截断到 {duration:.1f}s")
            end = duration
        if end <= start:
            fail(f"窗口非法: start={start:.1f}s ≥ end={end:.1f}s")
        win = end - start
        log(f"视频时长 {duration:.1f}s；抽帧窗口 [{fmt_ts(start)} – {fmt_ts(end)}]（{win:.1f}s）")

        # ---- 预算 N：budget → max-frames → 2fps → 100，逐层收紧 ----
        if str(args.budget).strip().lower() == "auto":
            focused = user_start is not None or user_end is not None
            budget = frame_budget(duration, start, end) if focused else frame_budget(duration)
            budget_desc = f"auto→{budget}"
        else:
            try:
                budget = int(args.budget)
            except ValueError:
                fail(f"--budget 非法: {args.budget!r}（应为 auto 或正整数）")
            if budget < 1:
                fail("--budget 必须 ≥ 1")
            budget_desc = f"手动 {budget}"

        fps_cap = HARD_FPS
        if args.fps is not None:
            if args.fps <= 0:
                fail(f"--fps 必须 > 0: {args.fps}")
            if args.fps > HARD_FPS:
                log(f"警告: --fps {args.fps} 超过全局硬上限 {HARD_FPS}，按 {HARD_FPS} 计")
            fps_cap = min(HARD_FPS, args.fps)
        cap_by_fps = max(1, int(win * fps_cap + 1e-9))

        hard_cap = HARD_FRAMES
        if args.max_frames is not None:
            if args.max_frames < 1:
                fail(f"--max-frames 必须 ≥ 1: {args.max_frames}")
            hard_cap = min(HARD_FRAMES, args.max_frames)

        n = max(1, min(budget, cap_by_fps, hard_cap))
        log(f"模式={args.mode}；预算={budget_desc}；2fps 上限→{cap_by_fps} 帧；"
            f"帧数硬上限→{hard_cap}；最终 N={n}")

        # ---- 选点 ----
        scene_pts = []
        if args.mode in ("auto", "scene"):
            log(f"场景检测中（阈值 {SCENE_TH}）…")
            scene_pts = detect_scene_times(ffmpeg, video, start, end)
            log(f"场景检测命中 {len(scene_pts)} 个切换点")
        points = plan_points(args.mode, scene_pts, start, end, n)
        if not points:
            fail("未能确定任何抽帧时间点")
        n_scene = sum(1 for _, s in points if s == "scene")
        log(f"选点完成：场景点 {n_scene} + 均匀点 {len(points) - n_scene} = {len(points)}")

        # ---- 输出目录（清理本脚本命名规则下的旧帧，避免新旧混杂）----
        frames_dir = Path(args.out_dir).expanduser() / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        for old in frames_dir.glob("????_t*.jpg"):
            old.unlink()
        frames_json = frames_dir / "frames.json"
        if frames_json.exists():
            frames_json.unlink()

        # ---- 并行抽帧（进程派生型 I/O，4 线程足够）----
        planned = []
        for i, (t, src) in enumerate(points):
            t_disp = float(f"{t:.1f}")  # 与文件名 08.1f 保持一致
            planned.append({"t": t, "t_disp": t_disp, "src": src,
                            "path": frames_dir / make_name(i, t_disp)})

        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(extract_one, ffmpeg, video, item["t"], args.width, item["path"]): i
                for i, item in enumerate(planned)
            }
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    ok, err = fut.result()
                except Exception as e:  # 兜底，不让单个帧炸掉整批
                    ok, err = False, f"{type(e).__name__}: {e}"
                results[i] = {**planned[i], "ok": ok, "err": err}
                done += 1
                if done % 10 == 0 or done == len(planned):
                    log(f"抽帧进度 {done}/{len(planned)}")

        ordered = [results[i] for i in sorted(results)]
        kept = [r for r in ordered if r["ok"]]
        for r in ordered:
            if not r["ok"]:
                log(f"警告: t={r['t']:.1f}s 抽帧失败（{r['err'][:160]}），已跳过")
        if not kept:
            fail("所有时间点抽帧均失败")

        # 有失败时按顺序重命名，消除序号空洞（新序号 ≤ 旧序号，顺序处理无冲突）
        if len(kept) < len(ordered):
            for new_i, r in enumerate(kept):
                new_path = frames_dir / make_name(new_i, r["t_disp"])
                if new_path != r["path"]:
                    os.replace(r["path"], new_path)
                    r["path"] = new_path

        # ---- frames.json（契约：file/t/source）----
        entries = [{"file": r["path"].name, "t": r["t_disp"], "source": r["src"]} for r in kept]
        frames_json.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"完成：{len(kept)} 帧 → {frames_dir}")

        emit_result({
            "ok": True,
            "count": len(kept),
            "frames_json": str(frames_json.resolve()),
            "window": {"start": round(start, 3), "end": round(end, 3)},
        })
    except SystemExit:
        raise
    except Exception as e:
        fail(f"未预期错误: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
