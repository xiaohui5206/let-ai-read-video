#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frames.py — video-watch 自适应抽帧 CLI

三种模式（--mode）：
  scene   仅用 ffmpeg 场景检测：-vf "select='gt(scene,0.3)',showinfo"，解析 showinfo 的 pts_time
  uniform 在 [start, end] 窗口均匀取 N 个“段中心”时间点
  auto    至少一半预算作为均匀时间骨架；其余场景点与补点按最大间距选择（默认）

预算 N：--budget auto 走 common.frame_budget；再受全局硬上限 2fps / 100 帧约束，
--fps / --max-frames 只能在此基础上继续收紧。

定向补帧：--times-json 接受 JSON 数组，或
{"version": 1, "pass_id": "...", "times": [...]}。数组条目可为数字，或
{"t": 12.3, "reason": "...", "source": "...", "width": 1024}。
配合 --append 可保留既有帧并合并索引；--pass-id 记录本轮来源。基础规划
仍限制 2fps；定向模式可显式使用 --fps 4，最小间隔 0.25 秒。

产物：<out-dir>/frames/{idx:04d}_t{秒:08.1f}.jpg + <out-dir>/frames/frames.json
stdout 最后一行：RESULT_JSON: {"ok": true, "count": N, "frames_json": ..., "window": {...}}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent  # skill 根目录

# ---------------------------------------------------------------------------
# 优先复用同目录 common.py；导入失败时启用最小兜底实现，保证脚本被
# 单独分发时仍可运行。
# ---------------------------------------------------------------------------
try:
    import common  # type: ignore

    find_tool = common.find_tool
    parse_time = common.parse_time
    frame_budget = common.frame_budget
    fmt_ts = common.fmt_ts
except Exception:  # pragma: no cover - common 缺失时的最小兜底实现

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


# 抽帧策略常量
MIN_GAP = 0.5       # 相邻帧最小间隔（秒）
SCENE_TH = 0.3      # 场景检测阈值 gt(scene,0.3)
HARD_FPS = 2.0      # 全局帧率硬上限
TARGETED_HARD_FPS = 4.0  # --times-json 定向补帧的局部帧率硬上限
TARGETED_MIN_GAP = 0.25  # 定向补帧最小间隔；基础规划仍使用 MIN_GAP
ACTUAL_COLLISION_GAP = 0.001  # 请求已限频；这里只合并落到同一实际帧的结果
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


def select_max_spacing(candidates, occupied, n, gap=MIN_GAP):
    """从候选时间中逐个选择离既有时间点最远的 n 个。

    这是一个确定性的 max-min 选择：每轮最大化候选点到已选点的最近距离；
    距离相同时优先较晚的时间点，避免补点长期偏向片头。所有点继续遵守
    ``gap`` 最小间隔。
    """
    if n <= 0:
        return []
    remaining = sorted({float(t) for t in candidates if math.isfinite(float(t))})
    anchors = [float(t) for t in occupied]
    selected = []

    while remaining and len(selected) < n:
        eligible = [
            t for t in remaining
            if all(abs(t - anchor) >= gap - 1e-9 for anchor in anchors)
        ]
        if not eligible:
            break

        def nearest_distance(t):
            if not anchors:
                return math.inf
            return min(abs(t - anchor) for anchor in anchors)

        chosen = max(eligible, key=lambda t: (nearest_distance(t), t))
        selected.append(chosen)
        anchors.append(chosen)
        remaining.remove(chosen)
    return selected


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

    # auto：先锁定至少 50% 的均匀时间骨架，确保全片（尤其片尾）有覆盖。
    backbone_n = max(1, (n + 1) // 2)
    kept = [(t, "uniform") for t in uniform_points(start, end, backbone_n)]

    # 剩余预算先给场景点，但不按输入顺序抢占；每轮选择离骨架/已选点最远者。
    slots = n - len(kept)
    scene_selected = select_max_spacing(scene_pts, [t for t, _ in kept], slots)
    kept.extend((t, "scene") for t in scene_selected)

    # 场景点不足时，从细网格继续用同一最大间距策略补齐。
    slots = n - len(kept)
    if slots > 0:
        grid_n = max(8 * n, 32)
        # 包含窗口起点、避开窗口终点；在 2fps 极限下仍能提供恰好相距
        # MIN_GAP 的候选，而不会因“段中心”少半格导致预算补不满。
        grid = [start + i * (end - start) / grid_n for i in range(grid_n)]
        filled = select_max_spacing(grid, [t for t, _ in kept], slots)
        kept.extend((t, "uniform") for t in filled)

    kept.sort(key=lambda x: x[0])
    return kept


def make_name(idx, t_disp):
    """契约命名：{idx:04d}_t{秒:08.1f}.jpg"""
    return f"{idx:04d}_t{t_disp:08.1f}.jpg"


def load_times_spec(path):
    """读取定向时间 JSON，返回 ``(points, envelope_pass_id)``。

    支持两种中立格式：

    * ``[12.3, {"t": 18.5, "reason": "...", "source": "..."}]``
    * ``{"version": 1, "pass_id": "r1", "times": [...]}``

    每个 point 规范化为含 t/source/reason/width 的字典。时间必须是有限数字；
    ``width`` 可选，语义与命令行 ``--width`` 相同。
    """
    spec_path = Path(path).expanduser()
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 --times-json {spec_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"--times-json 不是合法 JSON: {exc}") from exc

    envelope_pass_id = None
    if isinstance(data, list):
        raw_points = data
    elif isinstance(data, dict):
        raw_points = data.get("times")
        if not isinstance(raw_points, list):
            raise ValueError("--times-json 对象必须包含数组字段 times")
        if data.get("pass_id") is not None:
            envelope_pass_id = str(data["pass_id"])
    else:
        raise ValueError("--times-json 顶层必须是数组或含 times 数组的对象")

    points = []
    for idx, raw in enumerate(raw_points):
        if isinstance(raw, bool):
            raise ValueError(f"--times-json 第 {idx + 1} 项的时间必须是数字")
        if isinstance(raw, (int, float)):
            item = {"t": raw}
        elif isinstance(raw, dict):
            item = raw
        else:
            raise ValueError(f"--times-json 第 {idx + 1} 项必须是数字或对象")

        value = item.get("t")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"--times-json 第 {idx + 1} 项缺少数字字段 t")
        t = float(value)
        if not math.isfinite(t):
            raise ValueError(f"--times-json 第 {idx + 1} 项的 t 必须是有限数字")

        source = item.get("source", "targeted")
        reason = item.get("reason")
        if source is None:
            source = "targeted"
        if not isinstance(source, str):
            raise ValueError(f"--times-json 第 {idx + 1} 项的 source 必须是字符串")
        if reason is not None and not isinstance(reason, str):
            raise ValueError(f"--times-json 第 {idx + 1} 项的 reason 必须是字符串")

        width = item.get("width")
        if width is not None:
            if isinstance(width, bool) or not isinstance(width, (int, float)):
                raise ValueError(f"--times-json 第 {idx + 1} 项的 width 必须是整数")
            if not float(width).is_integer():
                raise ValueError(f"--times-json 第 {idx + 1} 项的 width 必须是整数")
            width = int(width)

        points.append({
            "t": t,
            "source": source or "targeted",
            "reason": reason,
            "width": width,
        })
    return points, envelope_pass_id


def dedupe_target_points(points, occupied=(), gap=MIN_GAP):
    """按请求顺序去掉与既有/已保留时间过近的定向点，返回 (kept, skipped)。"""
    anchors = [float(t) for t in occupied]
    kept = []
    skipped = 0
    for point in points:
        t = float(point["t"])
        if any(abs(t - anchor) < gap - 1e-9 for anchor in anchors):
            skipped += 1
            continue
        kept.append(point)
        anchors.append(t)
    kept.sort(key=lambda item: item["t"])
    return kept, skipped


def entry_time(entry):
    """读取索引条目的实际时间；兼容只有旧字段 t 的索引。"""
    value = entry.get("actual_t", entry.get("t"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"frames.json 条目缺少数字时间: {entry!r}")
    t = float(value)
    if not math.isfinite(t):
        raise ValueError(f"frames.json 条目时间不是有限数字: {entry!r}")
    return t


def load_existing_entries(frames_json):
    """读取并规范化既有 frames.json；旧条目自动补 requested_t/actual_t。"""
    if not frames_json.exists():
        return []
    try:
        data = json.loads(frames_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取既有 frames.json: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("既有 frames.json 顶层必须是数组")

    entries = []
    for raw in data:
        if not isinstance(raw, dict) or not isinstance(raw.get("file"), str):
            raise ValueError(f"frames.json 含非法条目: {raw!r}")
        item = dict(raw)
        actual = round(entry_time(item), 3)
        requested = item.get("requested_t", item.get("t", actual))
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            requested = actual
        item["t"] = actual
        item["requested_t"] = round(float(requested), 3)
        item["actual_t"] = actual
        item.setdefault("source", "unknown")
        item.setdefault("pass_id", "legacy")
        entries.append(item)
    # 请求阶段已限频；实际 PTS 会因视频时间基量化产生轻微偏移。这里只合并
    # 真正落到同一解码帧的结果，避免后续 pass 误删已有的 4fps 证据。
    return dedupe_entries(entries, gap=ACTUAL_COLLISION_GAP)


def dedupe_entries(entries, gap=MIN_GAP):
    """按 actual_t 排序并去重；相距小于 gap 时保留排序后的第一项。"""
    out = []
    for entry in sorted(entries, key=lambda item: (entry_time(item), item.get("file", ""))):
        t = entry_time(entry)
        if out and t - entry_time(out[-1]) < gap - 1e-9:
            continue
        out.append(entry)
    return out


def next_frame_index(entries):
    """从既有文件名推导下一个序号；无法解析时至少从条目数之后开始。"""
    indices = []
    for entry in entries:
        match = re.match(r"^(\d+)_t", Path(entry.get("file", "")).name)
        if match:
            indices.append(int(match.group(1)))
    return max(max(indices, default=-1) + 1, len(entries))


def make_entry(file_name, requested_t, actual_t, source, reason, pass_id, width):
    """构造兼容旧字段且带有定向补帧元数据的索引条目。"""
    actual = round(float(actual_t), 3)
    entry = {
        "file": file_name,
        "t": actual,
        "source": source,
        "requested_t": round(float(requested_t), 3),
        "actual_t": actual,
        "pass_id": pass_id,
    }
    if reason:
        entry["reason"] = reason
    if width is not None:
        entry["width"] = int(width)
    return entry


def write_json_atomic(path, data):
    """在目标同目录写临时文件，再用 os.replace 原子替换 JSON 索引。"""
    path = Path(path)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def extract_one(ffmpeg, video, t, width, out_path):
    """逐时间点精确抽帧：-ss 前置（关键帧定位+精确解码），失败回退 t-0.6s 重试一次。

    返回 ``(ok, err, actual_t)``。通过 showinfo 读取首个解码输出帧相对 seek
    点的 PTS，因此 actual_t 对应实际画面时刻，而不只是请求 seek 的时刻；
    showinfo 不可用时才回退为 seek 时间。scale 用 -2（等比且保证偶数尺寸，
    mjpeg/yuv420p 要求）。
    """
    attempts = [t]
    if t - 0.6 > 0:
        attempts.append(t - 0.6)
    last_err = ""
    for ts in attempts:
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
            "-ss", f"{ts:.3f}", "-i", video, "-frames:v", "1", "-an",
        ]
        filters = ["showinfo"]
        if width and width > 0:
            filters.append(f"scale={width}:-2")
        cmd += ["-vf", ",".join(filters)]
        cmd += ["-q:v", "3", "-y", str(out_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            last_err = "超时(180s)"
            continue
        if r.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            match = re.search(
                r"\bpts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
                r.stderr or "",
            )
            relative_pts = float(match.group(1)) if match else 0.0
            return True, "", float(ts) + relative_pts
        err_lines = (r.stderr or "").strip().splitlines()
        last_err = err_lines[-1] if err_lines else f"exit={r.returncode}"
    return False, last_err, None


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
    p.add_argument("--fps", type=float, default=None,
                   help="帧率上限：基础模式最高 2fps；--times-json 定向模式最高 4fps")
    p.add_argument("--width", type=int, default=512, help="输出帧宽度 px（默认 512，高度等比；≤0 不缩放）")
    p.add_argument("--mode", choices=["auto", "scene", "uniform"], default="auto",
                   help="抽帧模式：auto（默认，融合）/ scene（纯场景检测）/ uniform（纯均匀网格）")
    p.add_argument("--times-json", default=None,
                   help="定向时间 JSON：直接数组，或含 version/pass_id/times 的对象；指定后跳过自动选点")
    p.add_argument("--append", action="store_true",
                   help="保留既有 frames/ 与 frames.json，合并新增帧并按实际时间去重")
    p.add_argument("--pass-id", default=None,
                   help="本轮抽帧标识；优先于 --times-json 对象中的 pass_id")
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

        explicit_times = args.times_json is not None

        # ---- 模式上限：基础规划 2fps；定向 times-json 可显式提高到 4fps -----
        mode_hard_fps = TARGETED_HARD_FPS if explicit_times else HARD_FPS
        fps_cap = HARD_FPS
        if args.fps is not None:
            if args.fps <= 0:
                fail(f"--fps 必须 > 0: {args.fps}")
            if args.fps > mode_hard_fps:
                mode_name = "定向" if explicit_times else "基础"
                log(f"警告: {mode_name}模式 --fps {args.fps} 超过硬上限 "
                    f"{mode_hard_fps:g}，按 {mode_hard_fps:g} 计")
            fps_cap = min(mode_hard_fps, args.fps)
        cap_by_fps = max(1, int(win * fps_cap + 1e-9))

        hard_cap = HARD_FRAMES
        if args.max_frames is not None:
            if args.max_frames < 1:
                fail(f"--max-frames 必须 ≥ 1: {args.max_frames}")
            hard_cap = min(HARD_FRAMES, args.max_frames)

        # ---- 输出目录与既有索引 -------------------------------------------
        frames_dir = Path(args.out_dir).expanduser() / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames_json = frames_dir / "frames.json"
        if args.append:
            existing_entries = load_existing_entries(frames_json)
            log(f"追加模式：载入既有索引 {len(existing_entries)} 帧")
        else:
            existing_entries = []

        # ---- 选点：显式时间优先，否则走旧的预算/模式 ----------------------
        envelope_pass_id = None
        if explicit_times:
            try:
                points, envelope_pass_id = load_times_spec(args.times_json)
            except ValueError as exc:
                fail(str(exc))
            if not points:
                fail("--times-json 没有任何时间点")
            for point in points:
                t = point["t"]
                if t < start - 1e-9 or t > end + 1e-9:
                    fail(f"--times-json 时间 {t:.3f}s 超出窗口 [{start:.3f}, {end:.3f}]")
                if t >= duration - 1e-9:
                    fail(f"--times-json 时间 {t:.3f}s 位于或超出视频末尾 {duration:.3f}s")
            log(f"定向选点：从 {args.times_json} 读取 {len(points)} 项；"
                f"局部帧率上限 {fps_cap:g}fps")
        else:
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

            n = max(1, min(budget, cap_by_fps, hard_cap))
            log(f"模式={args.mode}；预算={budget_desc}；2fps 上限→{cap_by_fps} 帧；"
                f"帧数硬上限→{hard_cap}；最终 N={n}")

            scene_pts = []
            if args.mode in ("auto", "scene"):
                log(f"场景检测中（阈值 {SCENE_TH}）…")
                scene_pts = detect_scene_times(ffmpeg, video, start, end)
                log(f"场景检测命中 {len(scene_pts)} 个切换点")
            planned_points = plan_points(args.mode, scene_pts, start, end, n)
            points = [
                {"t": t, "source": source, "reason": None, "width": None}
                for t, source in planned_points
            ]
            n_scene = sum(1 for point in points if point["source"] == "scene")
            log(f"选点完成：场景点 {n_scene} + 均匀点 {len(points) - n_scene} = {len(points)}")

        pass_id = args.pass_id if args.pass_id is not None else envelope_pass_id
        if not pass_id:
            pass_id = "append" if args.append else ("targeted" if explicit_times else "base")

        # 显式/追加点同样遵守调用级 fps 与帧数上限；既有帧不计入本轮上限。
        requested_count = len(points)
        mode_min_gap = TARGETED_MIN_GAP if explicit_times else MIN_GAP
        dedupe_gap_value = max(mode_min_gap, 1.0 / fps_cap)
        occupied = [entry_time(entry) for entry in existing_entries]
        points, skipped_requested = dedupe_target_points(points, occupied, dedupe_gap_value)
        if skipped_requested:
            log(f"请求时间去重：跳过 {skipped_requested} 个与既有/本轮点过近的时间")
        if explicit_times and len(points) > min(cap_by_fps, hard_cap):
            fail(f"--times-json 去重后有 {len(points)} 点，超过本轮上限 "
                 f"{min(cap_by_fps, hard_cap)}")
        if not points:
            if not existing_entries:
                fail("去重后没有可抽取的时间点")
            write_json_atomic(frames_json, existing_entries)
            log(f"完成：没有新增帧；索引共 {len(existing_entries)} 帧 → {frames_dir}")
            emit_result({
                "ok": True,
                "count": len(existing_entries),
                "added": 0,
                "frames_json": str(frames_json.resolve()),
                "window": {"start": round(start, 3), "end": round(end, 3)},
                "append": bool(args.append),
                "pass_id": pass_id,
                "sampling_mode": "targeted" if explicit_times else "base",
                "fps_cap": fps_cap,
            })
            return

        # 非追加模式延迟到所有输入验证完成后再清理旧产物，避免参数错误破坏成功结果。
        if not args.append:
            for old in frames_dir.glob("????_t*.jpg"):
                old.unlink()
            for pending in frames_dir.glob(".pending_*.jpg"):
                pending.unlink()
            if frames_json.exists():
                frames_json.unlink()

        # ---- 并行抽帧（进程派生型 I/O，4 线程足够）----
        planned = []
        for i, point in enumerate(points):
            pending_path = frames_dir / f".pending_{os.getpid()}_{i:04d}.jpg"
            if pending_path.exists():
                pending_path.unlink()
            effective_width = point["width"] if point["width"] is not None else args.width
            planned.append({
                "requested_t": point["t"],
                "src": point["source"],
                "reason": point["reason"],
                "width": effective_width,
                "path": pending_path,
            })

        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(
                    extract_one, ffmpeg, video, item["requested_t"], item["width"], item["path"]
                ): i
                for i, item in enumerate(planned)
            }
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    ok, err, actual_t = fut.result()
                except Exception as e:  # 兜底，不让单个帧炸掉整批
                    ok, err, actual_t = False, f"{type(e).__name__}: {e}", None
                results[i] = {**planned[i], "ok": ok, "err": err, "actual_t": actual_t}
                done += 1
                if done % 10 == 0 or done == len(planned):
                    log(f"抽帧进度 {done}/{len(planned)}")

        ordered = [results[i] for i in sorted(results)]
        for r in ordered:
            if not r["ok"]:
                if r["path"].exists():
                    r["path"].unlink()
                log(f"警告: t={r['requested_t']:.1f}s 抽帧失败（{r['err'][:160]}），已跳过")
        successful = [r for r in ordered if r["ok"] and r["actual_t"] is not None]
        if not successful:
            fail("所有时间点抽帧均失败")

        # 回退或帧率量化会改变 actual_t。请求阶段已经执行 2/4fps 限频，
        # 此处只移除真正落到同一解码帧的结果，避免 30fps 等时间基把
        # 0.25s 请求量化成 0.233/0.267s 后误删有效帧。
        accepted = []
        actual_anchors = [entry_time(entry) for entry in existing_entries]
        for result in sorted(successful, key=lambda item: item["actual_t"]):
            actual_t = float(result["actual_t"])
            if any(abs(actual_t - anchor) < ACTUAL_COLLISION_GAP - 1e-9
                   for anchor in actual_anchors):
                if result["path"].exists():
                    result["path"].unlink()
                log(f"实际时间去重：跳过 t={actual_t:.3f}s")
                continue
            accepted.append(result)
            actual_anchors.append(actual_t)

        # 新文件从既有最大序号之后编号；文件名与 t 均使用实际成功时间。
        new_entries = []
        next_index = next_frame_index(existing_entries)
        for result in accepted:
            actual_t = float(result["actual_t"])
            t_disp = float(f"{actual_t:.1f}")
            final_path = frames_dir / make_name(next_index, t_disp)
            while final_path.exists():
                next_index += 1
                final_path = frames_dir / make_name(next_index, t_disp)
            os.replace(result["path"], final_path)
            new_entries.append(make_entry(
                final_path.name,
                result["requested_t"],
                actual_t,
                result["src"],
                result["reason"],
                pass_id,
                result["width"],
            ))
            next_index += 1

        # ---- frames.json（旧 file/t/source 字段保留，附加请求/实际时间）----
        entries = dedupe_entries(
            existing_entries + new_entries,
            ACTUAL_COLLISION_GAP,
        )
        write_json_atomic(frames_json, entries)
        log(f"完成：新增 {len(new_entries)} 帧，索引共 {len(entries)} 帧 → {frames_dir}")

        emit_result({
            "ok": True,
            "count": len(entries),
            "added": len(new_entries),
            "requested": requested_count,
            "frames_json": str(frames_json.resolve()),
            "window": {"start": round(start, 3), "end": round(end, 3)},
            "append": bool(args.append),
            "pass_id": pass_id,
            "sampling_mode": "targeted" if explicit_times else "base",
            "fps_cap": fps_cap,
        })
    except SystemExit:
        raise
    except Exception as e:
        fail(f"未预期错误: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
