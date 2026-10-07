#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py — video-watch 本地 Web 界面（零新增依赖，仅标准库 http.server）。

给命令行流水线（scripts/watch.py）套一个脱离 AI Agent 的网页壳：
浏览器表单 → 后台线程跑 watch.py 子进程 → 页面轮询实时日志 → 在线预览产物。

- 只监听 127.0.0.1（默认端口 8765，--port 可改，--port 0 自动分配）；
- 每个任务一个后台线程；batch 模式在单线程内逐 URL 串行；
- 子进程契约沿用脚本约定：日志在前、stdout 末行 RESULT_JSON: {...}、
  错误走 stderr + ok:false + 退出码 1（本模块把 stderr 并入日志流，倒序查找
  RESULT_JSON 不受影响）；
- 双通道进度（文字稿 / 关键帧）由日志锚点驱动，见 update_progress()；
  snapshot() 携带 progress，前端据此渲染两条独立进度条；
- POST /api/export_pdf 把任务关键帧导出为带时间标识的 关键帧.pdf
  （手写 PDF 生成器 scripts/make_frames_pdf.py，零第三方依赖）；
- 侧栏三功能：关键帧截取原则（--max-frames/--fps 透传 watch.py）、
  关键字定位关键帧（postprocess 调 frames.py --times-json --append）、
  关键帧去重（aHash 标记 dropped:true，不删文件）；
- /api/files 只放行仓库 runs/ 目录内的文件（resolve 后校验，防路径穿越）。

用法（任意 cwd 下）：
    python scripts/webui.py [--port 8765] [--no-browser]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 任意 cwd 下可 import common
import common  # noqa: E402
import webui_jobs  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent   # 仓库根目录
SCRIPTS_DIR = Path(__file__).resolve().parent         # scripts/ 目录
WEBUI_DIR = SKILL_ROOT / "webui"
INDEX_HTML = WEBUI_DIR / "index.html"
STATIC_DIR = WEBUI_DIR                                # /static/ 只允许此目录内文件
RUNS_ROOT = common.runs_dir().resolve()               # /api/files 的唯一放行根

RESULT_PREFIX = "RESULT_JSON: "
LOG_TAIL_LIMIT = 500        # 任务日志缓冲保留尾部行数
PARSE_TAIL_LIMIT = 300      # RESULT_JSON 倒序查找时保留的输出尾部行数
MAX_BODY_BYTES = 1 << 20    # POST 体上限 1 MB
PERSIST_INTERVAL = 0.5      # 运行中持久化节流（秒）；状态切换时强制落库
DB_LOG_TAIL = 50            # 落库的日志摘要行数

# T02 全局调度器（重型并发 1 / 轻型并发 2）；T01 持久化（main 启动时初始化）
SCHED = webui_jobs.Scheduler()
STORE: webui_jobs.JobStore | None = None

# 与 watch.py --item 选集表达式一致：'3'（单集）/ '3-7'（区间）/ 'all'（全部）
_ITEM_SPEC_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

_TEXT_EXTS = {".txt", ".srt", ".vtt", ".log", ".md"}
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


# ---------------------------------------------------------------- 命令拼装


def build_watch_command(url, want_transcript=True, want_frames=True, item=None,
                        frame_rule=None, width=None):
    """拼装 watch.py 子进程命令（list 形式，禁 shell）。

    产物选择 → --no-frames / --no-transcribe；WebUI 不使用审查包，固定
    --no-review；multi 模式透传 --item（'3' / '3-7' / 'all'）；
    width → --width（抽帧宽度，默认 None=512 由 watch.py 决定；U12）。
    frame_rule（关键帧截取原则，仅 want_frames 时生效）：
      {"type": "count", value: N}    → --budget N（目标 N 帧，仍受 2fps/100 硬上限）
      {"type": "interval", value: X} → 不在此拼参：watch.py 以 --no-frames 跑，
                                       抽帧由 webui 按显式时间点调 frames.py
                                       --times-json 执行（见 run_interval_frames）
      {"type": "default"} / None     → 不加参数
    """
    cmd = [sys.executable, str(SCRIPTS_DIR / "watch.py"), str(url)]
    if not want_frames:
        cmd.append("--no-frames")
    if not want_transcript:
        cmd.append("--no-transcribe")
    cmd.append("--no-review")
    if item:
        cmd += ["--item", str(item)]
    if width:
        cmd += ["--width", str(int(width))]
    if want_frames and frame_rule and frame_rule.get("type") == "count":
        cmd += ["--budget", str(int(frame_rule["value"]))]
    return cmd


# ---------------------------------------------------------------- RESULT 解析


def parse_result_json(output_text):
    """从子进程输出文本倒序找末行 RESULT_JSON: 并解析；找不到/非法 JSON 返回 None。"""
    for line in reversed(output_text.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):])
            except json.JSONDecodeError:
                return None
    return None


def extract_results(result, url):
    """把 watch.py 的 RESULT_JSON 统一展开为 results 列表。

    单集格式直接取字段；多P聚合格式 {ok, episodes, ...} 把 episodes[] 逐集展开
    （聚合记录没有 title/frame_count，标题以「第 N 集」兜底，frame_count 置 None）。
    url 一律脱敏展示（common.redact_url：去凭据/查询串/fragment，T05）。
    """
    results = []
    display_url = common.redact_url(url)
    episodes = result.get("episodes")
    if isinstance(episodes, list):
        for ep in episodes:
            frames_json = ep.get("frames_json")
            entry = {
                "url": display_url,
                "ok": bool(ep.get("ok")),
                "title": f"第 {ep.get('item')} 集",
                "run_dir": ep.get("run_dir"),
                "transcript_txt": ep.get("transcript_txt"),
                "transcript_source": ep.get("transcript_source"),
                "frames_dir": str(Path(frames_json).parent) if frames_json else None,
                "frames_json": frames_json,
                "frame_count": None,
                "has_video": ep.get("has_video"),
                "has_audio": ep.get("has_audio"),
            }
            if ep.get("error"):
                entry["error"] = common.redact_text_urls(ep["error"])
            results.append(entry)
        return results
    entry = {
        "url": display_url,
        "ok": bool(result.get("ok")),
        "title": result.get("title"),
        "run_dir": result.get("run_dir"),
        "transcript_txt": result.get("transcript_txt"),
        "transcript_source": result.get("transcript_source"),
        "frames_dir": result.get("frames_dir"),
        "frames_json": result.get("frames_json"),
        "frame_count": result.get("frame_count"),
        "has_video": result.get("has_video"),
        "has_audio": result.get("has_audio"),
    }
    if result.get("error"):
        entry["error"] = common.redact_text_urls(result["error"])
    results.append(entry)
    return results


# ---------------------------------------------------------------- 进度解析
#
# 进度由任务日志锚点驱动（anchor → intra/stage），锚点取自
# watch.py / transcribe.py / frames.py 的实际日志行：
#   文字稿：1/4 探测输入→5，2/4 下载→15，3/4 转写→45，
#           加载 faster-whisper 模型 / 解析字幕文件→55，
#           转写完成 / 字幕解析完成→85，已写出:→95，✅ 完成→100(done)
#   关键帧：4/4 抽帧 / 视频时长→5，场景检测中→8，选点完成 / 定向选点→12，
#           抽帧进度 d/N→12+83×d/N，完成：新增 X 帧→98，✅ 完成→100(done)
# `3/4 转写：--no-transcribe，跳过` / `4/4 抽帧：--no-frames，跳过`
# 是跳过行，不触发通道开始。
#
# C05 批次加权：channel["intra"] 是"当前项内"锚点进度（0–100）；
# 总体 percent = (已完成项数 + intra/100) / 总项数。批量总项数 = URL 数；
# 多集从日志 `── 第 N 集（i/total）───` 或聚合 RESULT 推断，未知时显示不定态。
# ✅/RESULT ok 不直接置 100%：frames 通道还有后处理（关键字/去重/间隔抽帧）时
# 保持 running，一切步骤结束后才由 _finalize_job_progress 收尾。

_FRAME_PROGRESS_RE = re.compile(r"抽帧进度\s+(\d+)\s*/\s*(\d+)")
_FRAME_PLANNED_RE = re.compile(r"选点完成.*?=\s*(\d+)")
_FRAME_TARGETED_RE = re.compile(r"定向选点.*?读取\s*(\d+)\s*项")
_FRAME_DONE_RE = re.compile(r"完成：新增\s*(\d+)\s*帧")
# "2/4" 需后随空白：排除"抽帧进度 2/40"这类子串误命中
_DOWNLOAD_STEP_RE = re.compile(r"2/4\s")
# 多集：`── 第 3 集（2/5）───` → 集号/序号/总数
_EPISODE_RE = re.compile(r"第\s*(\d+)\s*集（(\d+)\s*/\s*(\d+)）")


def _new_channel(selected):
    """任务创建时的通道初始态：已选 → running(0%)；未选 → skipped。"""
    return {"state": "running" if selected else "skipped",
            "percent": 0, "intra": 0.0,
            "stage": "排队中" if selected else "未选择"}


def _weighted_percent(job, channel):
    """总体进度 = (已完成项数 + 当前项内进度/100) / 总项数；总数未知退回当前项内进度。"""
    if job.items_total:
        done = min(job.items_done, job.items_total)
        pct = (done + channel["intra"] / 100.0) / job.items_total * 100.0
        return round(min(100.0, pct), 1)
    return channel["intra"]


def _advance(job, channel, intra, stage):
    """推进通道：intra 单调不减；锚点命中才更新 stage 并重算加权 percent。"""
    if intra > channel["intra"]:
        channel["intra"] = intra
    channel["stage"] = stage
    channel["percent"] = _weighted_percent(job, channel)


def update_progress(job, line):
    """按日志锚点推进进度条（run_job / 抽帧步骤每读一行子进程输出调用一次）。

    只推进本任务已选且仍在 running 的通道；未选通道（skipped）永不更新。
    """
    done_line = "✅ 完成" in line
    with job._lock:
        # 多集：从 `── 第 N 集（i/total）───` 推断总数并进入新一项
        m_ep = _EPISODE_RE.search(line)
        if m_ep:
            job.items_total = int(m_ep.group(3))
            job.current_item = int(m_ep.group(2))
            job.items_done = int(m_ep.group(2)) - 1
            for key, selected in (("transcript", job.want_transcript),
                                  ("frames", job.want_frames)):
                ch = job.progress[key]
                if selected and ch["state"] == "running":
                    ch["intra"] = 0.0
                    ch["stage"] = f"第 {job.current_item}/{job.items_total} 集"
                    ch["percent"] = _weighted_percent(job, ch)
            return
        tr = job.progress["transcript"]
        if job.want_transcript and tr["state"] == "running":
            if "转写：无音频流" in line or "转写：缓存中无纯音频文件" in line:
                # C02：想转写但无音轨 → 通道明确"已跳过"，不再假 100%
                tr["state"] = "skipped"
                tr["stage"] = "已跳过：无音轨"
                tr["percent"] = _weighted_percent(job, tr)
            elif "1/4 探测输入" in line:
                _advance(job, tr, 5, "探测输入")
            elif _DOWNLOAD_STEP_RE.search(line):
                _advance(job, tr, 15, "下载视频")
            elif "3/4 转写" in line and "跳过" not in line:
                _advance(job, tr, 45, "转写中")
            elif "加载 faster-whisper 模型" in line or "解析字幕文件" in line:
                _advance(job, tr, 55, "加载模型 / 解析字幕")
            elif "转写完成" in line or "字幕解析完成" in line:
                _advance(job, tr, 85, "转写完成")
            elif "已写出:" in line:
                _advance(job, tr, 95, "写出文字稿")
        fr = job.progress["frames"]
        if job.want_frames and fr["state"] == "running":
            m_progress = _FRAME_PROGRESS_RE.search(line)
            m_done = _FRAME_DONE_RE.search(line)
            if "抽帧：输入无视频流" in line:
                # C02：想抽帧但无视频流（纯音频）→ 通道明确"已跳过"
                fr["state"] = "skipped"
                fr["stage"] = "已跳过：无视频流"
                fr["percent"] = _weighted_percent(job, fr)
            elif ("4/4 抽帧" in line and "跳过" not in line) or "视频时长" in line:
                _advance(job, fr, 5, "准备抽帧")
            elif "场景检测中" in line:
                _advance(job, fr, 8, "场景检测中")
            elif "选点完成" in line:
                m = _FRAME_PLANNED_RE.search(line)
                _advance(job, fr, 12,
                         f"选点完成（计划 {m.group(1)} 帧）" if m else "选点完成")
            elif "定向选点" in line:
                m = _FRAME_TARGETED_RE.search(line)
                _advance(job, fr, 12,
                         f"定向选点（{m.group(1)} 点）" if m else "定向选点")
            elif m_progress:
                done_n, total = int(m_progress.group(1)), int(m_progress.group(2))
                pct = 12 + round(83 * done_n / total) if total > 0 else 12
                _advance(job, fr, pct, f"抽帧 {done_n}/{total}")
            elif m_done:
                _advance(job, fr, 98, f"抽帧完成（新增 {m_done.group(1)} 帧）")
            elif "完成：没有新增帧" in line:
                _advance(job, fr, 98, "抽帧完成")
        if done_line:
            # ✅ 完成 = 当前项流水线走完：计入 items_done；intra 归零（该项已计入）。
            # 100%/done 只在【最后项完成】且【该通道无后处理】时置位（C05）；
            # 有后处理的通道显示 99% + "整理结果…"，由 _finalize_job_progress 收尾。
            job.items_done = max(job.items_done, job.current_item)
            for key, selected, has_pp in (
                    ("transcript", job.want_transcript, False),
                    ("frames", job.want_frames, job.frames_postprocess_pending())):
                ch = job.progress[key]
                if not selected or ch["state"] != "running":
                    continue
                final_item = bool(job.items_total) and job.items_done >= job.items_total
                if final_item and not has_pp:
                    ch["intra"] = 100.0
                    ch["percent"] = 100.0
                    ch["stage"] = "完成"
                    ch["state"] = "done"
                elif final_item:
                    ch["intra"] = 0.0
                    ch["percent"] = 99.0
                    ch["stage"] = "整理结果…"
                else:
                    ch["intra"] = 0.0
                    ch["percent"] = _weighted_percent(job, ch)
                    if job.items_total:
                        ch["stage"] = f"第 {job.items_done}/{job.items_total} 项完成"


def _finalize_job_progress(job, status):
    """含后处理的最终状态按各通道产物汇总；失败不得被早期完成覆盖。
    合理跳过保持灰色，取消中的运行通道收尾为已取消。"""
    with job._lock:
        for key, selected in (("transcript", job.want_transcript),
                              ("frames", job.want_frames)):
            ch = job.progress[key]
            if not selected:
                continue
            arts = [r.get("artifacts", {}).get(key, {}).get("status") for r in job.results]
            if status in ("done", "partial", "error") and arts and all(arts):
                failures = arts.count("failed")
                if failures:
                    ch.update(state="error", stage="部分失败" if failures < len(arts) else "失败",
                              percent=100.0 * (len(arts)-failures) / len(arts))
                elif all(a == "skipped" for a in arts):
                    ch.update(state="skipped", stage="已跳过", percent=0.0)
                else:
                    ch.update(state="done", stage="完成", percent=100.0, intra=100.0)
                continue
            if ch["state"] != "running":
                continue
            if status in ("done", "partial"):
                ch["intra"] = 100.0
                ch["percent"] = 100.0
                ch["stage"] = "完成" if status == "done" else "完成（部分失败）"
                ch["state"] = "done"
            elif status == "cancelled":
                ch["stage"] = "已取消"
                ch["state"] = "skipped"
            else:
                ch["stage"] = "失败"
                ch["state"] = "error"


# ---------------------------------------------------------------- 任务模型


class Job:
    """一个后台任务：single=1 个 URL；multi=1 个 URL + --item；batch=多 URL 串行。"""

    def __init__(self, mode, urls, want_transcript, want_frames, item=None,
                 frame_rule=None, keywords=None, dedup=None, frame_width=512):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.urls = list(urls)
        self.want_transcript = want_transcript
        self.want_frames = want_frames
        self.item = item
        # 关键帧截取原则 {type: default|count|interval, value}（仅 want_frames 时生效）
        self.frame_rule = frame_rule or {"type": "default"}
        # 关键字定位关键帧：关键字列表（空 = 不启用）
        self.keywords = list(keywords or [])
        # 关键帧去重：{enabled: bool, threshold: float}
        self.dedup = dedup or {"enabled": False, "threshold": 0.95}
        # U12 抽帧宽度（512 默认 / 768 / 1024）
        self.frame_width = int(frame_width)
        self.status = "queued"       # queued|running|cancelling|cancelled|done|partial|error|interrupted
        self.error = None
        self.results = []
        # T01/T03：创建时间、媒体键（归组与复用）、持久化节流
        self.created_at = webui_jobs.now_iso()
        self.finished_at = None
        first_input = urls[0]["url"] if urls and isinstance(urls[0], dict) else (urls[0] if urls else "")
        first_item = urls[0].get("item") if urls and isinstance(urls[0], dict) else item
        self.media_key = webui_jobs.media_key(first_input, first_item) if first_input and len(urls) == 1 else None
        self._last_persist = 0.0
        # C05 批次进度：总项数（multi 未知时为 None，由日志/聚合 RESULT 推断）、
        # 已完成项数、当前项序号
        self.items_total = None if mode == "multi" else len(urls)
        self.items_done = 0
        self.current_item = 1
        # 双通道进度：{transcript, frames} × {state, percent, intra, stage}；
        # state: idle|running|done|error|skipped（未选通道创建即 skipped）
        self.progress = {"transcript": _new_channel(want_transcript),
                         "frames": _new_channel(want_frames)}
        self._logs = deque(maxlen=LOG_TAIL_LIMIT)
        self._lock = threading.Lock()
        # 产物后处理钩子（预留，见 run_postprocess）：None 表示未挂接
        self.postprocess = None

    def frames_postprocess_pending(self):
        """frames 通道在 watch 之后仍有步骤：关键字补帧 / 去重 / 间隔抽帧。"""
        return self.want_frames and (bool(self.keywords) or bool(self.dedup.get("enabled"))
                                     or self.frame_rule.get("type") == "interval")

    def params_dict(self):
        """完整任务参数（持久化 / 重试用）。"""
        return {"mode": self.mode, "urls": self.urls,
                "want_transcript": self.want_transcript,
                "want_frames": self.want_frames, "item": self.item,
                "frame_rule": self.frame_rule, "keyword": " ".join(self.keywords),
                "dedup": self.dedup, "frame_width": self.frame_width}

    def log(self, msg):
        with self._lock:
            self._logs.append(str(msg))

    def add_results(self, entries):
        with self._lock:
            self.results.extend(entries)

    def finish(self, status, error=None):
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = webui_jobs.now_iso()

    def snapshot(self):
        with self._lock:
            progress = {}
            for k, v in self.progress.items():
                ch = dict(v)
                # C05：multi 总数未知（all 展开前）不给确定百分比，前端显示不定态
                if (self.mode == "multi" and self.items_total is None
                        and ch["state"] == "running"):
                    ch["percent"] = None
                progress[k] = ch
            return {
                "ok": True,
                "job_id": self.id,
                "status": self.status,
                "log": list(self._logs),
                "results": refreshed_results(self.results),
                "progress": progress,
                "error": self.error,
                "params": self.params_dict(),
                "media_key": self.media_key,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "queue_position": SCHED.queue_position(self.id),
            }


JOBS = {}
JOBS_LOCK = threading.Lock()


def persist_job(job, force=False):
    """T01：把任务状态落库（sqlite）。运行中节流 PERSIST_INTERVAL 秒；状态切换强制。"""
    if STORE is None:
        return
    now = time.monotonic()
    with job._lock:
        # 只更新节流时间戳；snapshot() 自带锁，不能在持锁状态调用（Lock 不可重入）
        if not force and now - job._last_persist < PERSIST_INTERVAL:
            return
        job._last_persist = now
    snap = job.snapshot()
    try:
        STORE.upsert({
            "job_id": job.id, "mode": job.mode,
            "params": json.dumps(job.params_dict(), ensure_ascii=False),
            "status": job.status,
            "progress": json.dumps(snap["progress"], ensure_ascii=False),
            "results": json.dumps(snap["results"], ensure_ascii=False),
            "logs": json.dumps(snap["log"][-DB_LOG_TAIL:], ensure_ascii=False),
            "error": job.error, "media_key": job.media_key,
            "created_at": job.created_at, "finished_at": job.finished_at,
        })
    except Exception:     # 持久化失败不影响任务执行
        pass


def refreshed_results(results):
    """帧索引是恢复操作的持久事实；历史/刷新也据此更新显示数量。"""
    results = json.loads(json.dumps(results))
    for result in results:
        result["availability"] = artifact_availability(result)
        if not result.get("dedup") or not result.get("run_dir"):
            continue
        try:
            frames = json.loads((Path(result["run_dir"]) / "frames" / "frames.json").read_text(encoding="utf-8"))
            if not isinstance(frames, list):
                continue
            total = len(frames)
            dropped = sum(bool(f.get("dropped")) for f in frames if isinstance(f, dict))
            result["dedup"].update(total_count=total, kept_count=total-dropped, dropped_count=dropped)
            if result.get("friendly"):
                result["friendly"]["frame_count"] = total-dropped
        except (OSError, ValueError):
            pass
    return results


def artifact_availability(result):
    """资源当前是否可预览；只返回 runs 根内的实际文件，不改历史执行状态。"""
    friendly = result.get("friendly") or {}
    def available_file(raw):
        p = resolve_runs_path(raw) if raw else None
        return str(p) if p is not None and p.is_file() else None
    transcript = next((p for p in (available_file(friendly.get("transcript_txt")),
                                  available_file(result.get("transcript_txt"))) if p), None)
    index = available_file(result.get("frames_json"))
    frame_exists = False
    if index:
        try:
            entries = json.loads(Path(index).read_text(encoding="utf-8"))
            frame_exists = isinstance(entries, list) and any(
                isinstance(e, dict) and e.get("file") and
                available_file(Path(index).parent / e["file"]) for e in entries)
        except (OSError, ValueError, TypeError):
            pass
    return {"transcript": bool(transcript), "transcript_path": transcript,
            "frames_path": str(Path(index).parent) if frame_exists else None,
            "frames": frame_exists, "pdf": frame_exists,
            "reason": "文件已移动或不存在"}


def db_row_snapshot(row):
    """把 JobStore 行转成与 Job.snapshot() 同形的 API 快照。"""
    return {"ok": True, "job_id": row["job_id"], "status": row["status"],
            "log": row.get("logs") or [], "results": refreshed_results(row.get("results") or []),
            "progress": row.get("progress") or {}, "error": row.get("error"),
            "params": row.get("params"), "media_key": row.get("media_key"),
            "created_at": row.get("created_at"), "finished_at": row.get("finished_at"),
            "queue_position": None}


# T03 同源媒体写锁：同一 media_key 的任务串行，防止并发重复下载同一资源
_MEDIA_LOCKS = {}
_MEDIA_LOCKS_GUARD = threading.Lock()


def media_lock(key):
    with _MEDIA_LOCKS_GUARD:
        return _MEDIA_LOCKS.setdefault(key, threading.Lock())


def _write_json_atomic(path, data):
    """同目录写临时文件再 os.replace（项目惯例原子写）。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- 自定义间隔抽帧（C03）


def interval_points(duration, interval):
    """严格均匀时间点：start(0) + k×interval，t < duration（含起点 0）。

    frames.py 定向模式拒绝 t ≥ duration - 1e-9 的点，这里留 1ms 余量。
    20 秒视频、5 秒间隔 → 恰好 [0, 5, 10, 15]。
    """
    duration = float(duration)
    interval = float(interval)
    if duration <= 0 or interval <= 0:
        return []
    pts = []
    t = 0.0
    while t < duration - 1e-3:
        pts.append(round(t, 3))
        t += interval
    return pts


def _run_frames_step(job, cmd):
    """Popen 流式跑 frames.py：行入日志并驱动进度条，返回 RESULT_JSON dict 或 None。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
    except OSError as exc:
        job.log(f"[webui] frames.py 启动失败: {exc}")
        return None
    SCHED.register_proc(job.id, proc)
    tail = deque(maxlen=PARSE_TAIL_LIMIT)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            tail.append(line)
            job.log(line)
            update_progress(job, line)
        proc.wait()
    finally:
        SCHED.register_proc(job.id, None)
    return parse_result_json("\n".join(tail))


def _run_cancellable_command(job, cmd, timeout=30, **kwargs):
    """后处理子进程也归调度器管理，取消和超时均清理本进程树。"""
    kwargs.pop("capture_output", None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    SCHED.register_proc(job.id, proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except BaseException:
        webui_jobs._kill_tree(proc)
        proc.communicate()
        raise
    finally:
        SCHED.register_proc(job.id, None)


def run_interval_frames(job, entry):
    """C03 间隔模式抽帧：显式时间点走 frames.py --times-json 基础轮（禁场景点混入）。

    超 frames.py 定向硬上限 min(2fps折算, 100 帧) 时截断并记日志；
    结果回写 entry 的 frames_json/frames_dir/frame_count；失败记 entry["frames_error"]。
    """
    run_dir = Path(entry["run_dir"])
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        job.log(f"[webui] {run_dir.name}: manifest 读取失败，间隔抽帧跳过: {exc}")
        entry["frames_error"] = "manifest 读取失败"
        return
    duration = manifest.get("duration")
    video_path = manifest.get("video_path")
    if not duration or not video_path or not Path(video_path).is_file():
        job.log(f"[webui] {run_dir.name}: 时长/视频缺失，间隔抽帧跳过")
        entry["frames_error"] = "时长未知或视频缺失"
        return
    interval = float(job.frame_rule["value"])
    pts = interval_points(duration, interval)
    cap = max(1, min(100, int(float(duration) * 2)))   # 与 frames.py 定向硬上限一致
    if len(pts) > cap:
        job.log(f"[webui] 间隔抽帧计划 {len(pts)} 帧超出 2fps/100 帧硬上限，截断到前 {cap} 帧")
        pts = pts[:cap]
    if not pts:
        entry["frames_error"] = "无可抽帧时间点"
        return
    times_path = run_dir / "interval_times.json"
    _write_json_atomic(times_path, {
        "version": 1, "pass_id": "base",
        "times": [{"t": t, "reason": f"interval: 每{interval:g}s"} for t in pts],
    })
    job.log(f"[webui] 间隔抽帧：每 {interval:g}s 一帧，共 {len(pts)} 点 → {run_dir.name}")
    cmd = [sys.executable, str(SCRIPTS_DIR / "frames.py"),
           "--video", str(video_path), "--out-dir", str(run_dir),
           "--times-json", str(times_path), "--pass-id", "base",
           "--width", str(job.frame_width)]
    result = _run_frames_step(job, cmd)
    if result and result.get("ok"):
        entry["frames_json"] = result.get("frames_json")
        if result.get("frames_json"):
            entry["frames_dir"] = str(Path(result["frames_json"]).parent)
        entry["frame_count"] = result.get("count", len(pts))
        job.log(f"[webui] 间隔抽帧完成：{result.get('count')} 帧")
    else:
        err = (result or {}).get("error") or "frames.py 未输出 RESULT_JSON"
        entry["frames_error"] = err
        job.log(f"[webui] 间隔抽帧失败: {err}")


# ---------------------------------------------------------------- 关键字定位关键帧

KEYWORD_PASS_ID = "kw1"         # 关键字补帧轮次标识
KEYWORD_MAX_HITS = 60           # 命中点上限（超出均匀抽稀）


def find_keyword_times(segments, keywords):
    """在转写 segments 中定位关键字 → [(t_mid, keyword), ...] 按时间升序。

    - 大小写不敏感匹配 segment["text"]；一个 segment 命中多个关键字只取一次；
    - 时间点取命中 segment 的中点 (start+end)/2；
    - 相邻 <1s 的命中去重（保留先者）；
    - 超过 KEYWORD_MAX_HITS 个命中则均匀抽稀到 60。
    """
    report = build_keyword_report(segments, keywords)
    return [(h["t"], h["keywords"][0]) for h in report["hits"] if h["sampled"]]


def build_keyword_report(segments, keywords):
    """保留全部匹配句子与跳过原因；同句多词合并，句子中点不是逐词时间。"""
    kws = list(dict.fromkeys(k.lower() for k in keywords if k))
    hits = []
    for index, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        sentence = str(seg.get("text") or "")
        matched = [kw for kw in kws if kw in sentence.lower()]
        if not matched:
            continue
        start, end = seg.get("start"), seg.get("end")
        valid = (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                     and math.isfinite(v) for v in (start, end))
                 and 0 <= start <= end)
        def context(i):
            s = segments[i] if 0 <= i < len(segments) else None
            return str(s.get("text") or "") if isinstance(s, dict) else ""
        hits.append({"segment_index": index, "sentence": sentence, "keywords": matched,
                     "t": (float(start) + float(end)) / 2 if valid else None,
                     "context_before": context(index - 1), "context_after": context(index + 1),
                     "sampled": False, "frame_taken": False, "frame": None,
                     "reason": "pending" if valid else "invalid_time"})
    hits.sort(key=lambda h: h["t"] if h["t"] is not None else math.inf)
    candidates = []
    for hit in hits:
        if hit["t"] is None:
            continue
        if candidates and hit["t"] - candidates[-1]["t"] < 1.0 - 1e-9:
            hit["reason"] = "too_close"
        else:
            candidates.append(hit)
    selected = set(range(len(candidates)))
    if len(candidates) > KEYWORD_MAX_HITS:
        selected = {round(i * (len(candidates) - 1) / (KEYWORD_MAX_HITS - 1))
                    for i in range(KEYWORD_MAX_HITS)}
    for index, hit in enumerate(candidates):
        hit["sampled"] = index in selected
        hit["reason"] = "pending" if hit["sampled"] else "sample_limit"
    return {"version": 1, "position_method": "segment_midpoint", "keywords": kws,
            "total_matched": len(hits), "candidate_count": len(candidates),
            "sampled_count": len(selected), "added_count": 0, "hits": hits}


def _save_keyword_report(r, run_dir, report, reason=None, frames_json=None, previous_files=()):
    """以实际帧索引核对结果，失败和零命中也写报告。"""
    if reason == "extraction_failed":
        r["frames_error"] = "关键词补帧失败（详见日志与命中报告）"
    entries = []
    if frames_json:
        try:
            entries = json.loads(Path(frames_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if not isinstance(entries, list):
        entries = []
    index_changed = False
    for hit in report["hits"]:
        if not hit["sampled"]:
            continue
        hit["reason"] = reason or "frame_unavailable"
        if reason:
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("file"):
                continue
            requested = entry.get("requested_t", entry.get("t"))
            actual = entry.get("actual_t", entry.get("t"))
            if any(isinstance(t, (int, float)) and abs(t - hit["t"]) < 0.25 + 1e-9
                   for t in (requested, actual)) and (run_dir / "frames" / entry["file"]).is_file():
                hit.update(frame_taken=True, frame=entry["file"],
                           reason="reused" if entry["file"] in previous_files else "added")
                # 命中复用基础帧时也应保护，不能只保护新建的 kw1 帧。
                if not entry.get("keyword_locked"):
                    entry["keyword_locked"] = True
                    index_changed = True
                break
    if index_changed:
        _write_json_atomic(Path(frames_json), entries)
    path = run_dir / "keyword_report.json"
    _write_json_atomic(path, report)
    r["keyword_report"] = str(path.resolve())
    r["keyword"]["total_matched"] = report["total_matched"]


def _sync_manifest_keyword(run_dir, frames_result, keywords):
    """C07：关键字补帧后原子更新 manifest.json（count 同步 + passes upsert kw1，幂等）。

    相同参数重跑：frames.py 按时间去重 → added=0、count 不变；kw1 pass 记录替换
    而非追加，不重复计数。
    """
    mp = Path(run_dir) / "manifest.json"
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    frames = manifest.setdefault("frames", {})
    count = frames_result.get("count")
    if count is not None:
        frames["count"] = count
    frames.setdefault("base_count", 0)
    added = int(frames_result.get("added", 0))
    passes = frames.setdefault("passes", [])
    passes[:] = [p for p in passes
                 if not (isinstance(p, dict) and p.get("pass_id") == KEYWORD_PASS_ID)]
    passes.append({"pass_id": KEYWORD_PASS_ID, "count": added,
                   "keyword": "、".join(keywords),
                   "reason": "webui 关键字定位补帧"})
    _write_json_atomic(mp, manifest)


def keyword_lock_frames(job):
    """关键字锁帧 postprocess：按关键字回归文字稿时间位置，frames.py 定向补帧。

    对每个 ok 的 result：读 <run_dir>/manifest.json 取 video_path、读
    <run_dir>/transcript.json 取 segments；缺任一记日志跳过。命中点写
    keyword_times.json 后调 frames.py --times-json --append --pass-id kw1
    （即便任务未选生成关键帧也照常执行，关键字帧独立产出到 frames/）。
    结果写入 result["keyword"] = {matched, added}；0 命中记日志不报错。
    """
    for r in job.results:
        if not r.get("ok") or not r.get("run_dir"):
            continue
        run_dir = Path(r["run_dir"])
        manifest_path = run_dir / "manifest.json"
        transcript_path = run_dir / "transcript.json"
        if not manifest_path.is_file() or not transcript_path.is_file():
            job.log(f"[webui] {run_dir.name}: 缺 manifest.json/transcript.json，跳过关键字锁帧")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            segments = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            job.log(f"[webui] {run_dir.name}: manifest/transcript 读取失败，跳过: {exc}")
            continue
        video_path = manifest.get("video_path")
        if not isinstance(segments, list):
            job.log(f"[webui] {run_dir.name}: transcript.json 顶层不是数组，跳过")
            continue
        report = build_keyword_report(segments, job.keywords)
        hits = [(h["t"], h["keywords"][0]) for h in report["hits"] if h["sampled"]]
        r["keyword"] = {"matched": len(hits), "added": 0}
        if not video_path or not Path(video_path).is_file():
            job.log(f"[webui] {run_dir.name}: video_path 不存在，跳过关键字锁帧")
            _save_keyword_report(r, run_dir, report, reason="no_video")
            continue
        if not hits:
            job.log(f"[webui] {run_dir.name}: 关键字 {job.keywords} 0 命中")
            _save_keyword_report(r, run_dir, report)
            continue
        times_path = run_dir / "keyword_times.json"
        _write_json_atomic(times_path, {
            "version": 1,
            "times": [{"t": round(t, 3), "reason": f"keyword: {kw}"} for t, kw in hits],
        })
        # 沿用 frames.json 里已有帧的 width，没有则默认 512
        width = job.frame_width
        previous_files = set()
        frames_json = r.get("frames_json") or str(run_dir / "frames" / "frames.json")
        try:
            existing = json.loads(Path(frames_json).read_text(encoding="utf-8"))
            if isinstance(existing, list):
                previous_files = {e.get("file") for e in existing if isinstance(e, dict)}
                for e in existing:
                    if isinstance(e, dict) and isinstance(e.get("width"), int):
                        width = e["width"]
                        break
        except (OSError, json.JSONDecodeError):
            pass
        cmd = [sys.executable, str(SCRIPTS_DIR / "frames.py"),
               "--video", str(video_path), "--out-dir", str(run_dir),
               "--times-json", str(times_path), "--append",
               "--pass-id", KEYWORD_PASS_ID, "--width", str(width)]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        job.log(f"[webui] 关键字总命中 {report['total_matched']} 句，取样 {len(hits)} 处，定向补帧 → {run_dir.name}")
        try:
            proc = _run_cancellable_command(job, cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  env=env, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as exc:
            job.log(f"[webui] 关键字补帧失败（已忽略）: {exc}")
            _save_keyword_report(r, run_dir, report, reason="extraction_failed")
            continue
        if SCHED.is_cancelled(job.id):
            return
        result = parse_result_json(proc.stdout or "")
        if not result or not result.get("ok"):
            err = (result or {}).get("error") or f"退出码 {proc.returncode}"
            job.log(f"[webui] 关键字补帧失败（已忽略）: {err}")
            _save_keyword_report(r, run_dir, report, reason="extraction_failed")
            continue
        added = int(result.get("added", 0))
        r["keyword"] = {"matched": len(hits), "added": added}
        report["added_count"] = added
        _save_keyword_report(r, run_dir, report, frames_json=result.get("frames_json") or frames_json,
                             previous_files=previous_files)
        # 补帧后帧总数变化：同步 result 的 frame_count / frames_json / manifest（C07）
        if result.get("count") is not None:
            r["frame_count"] = result["count"]
        if result.get("frames_json"):
            r["frames_json"] = result["frames_json"]
            r["frames_dir"] = str(Path(result["frames_json"]).parent)
        _sync_manifest_keyword(run_dir, result, job.keywords)
        job.log(f"[webui] 关键字补帧完成：新增 {added} 帧")


# ---------------------------------------------------------------- 关键帧去重（aHash）

GRAY_SIDE = 16                  # 16x16 灰度 → 256 字节
GRAY_BYTES = GRAY_SIDE * GRAY_SIDE


def _extract_gray16(ffmpeg, img_path, job=None):
    """ffmpeg 把帧图缩放到 16x16 抽灰度原始字节；失败返回 None。"""
    cmd = [ffmpeg, "-v", "error", "-i", str(img_path),
           "-s", f"{GRAY_SIDE}x{GRAY_SIDE}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        proc = (_run_cancellable_command(job, cmd, timeout=30) if job else
                subprocess.run(cmd, capture_output=True, timeout=30))
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or len(proc.stdout) != GRAY_BYTES:
        return None
    return proc.stdout


def _extract_gray64(ffmpeg, img_path, job=None):
    """较细网格用于保护 PPT 的局部文字变化；失败时不自动隐藏该帧。"""
    try:
        cmd = [ffmpeg, "-v", "error", "-i", str(img_path),
               "-vf", "scale=64:64", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        proc = (_run_cancellable_command(job, cmd, timeout=30) if job else
                subprocess.run(cmd, capture_output=True, timeout=30))
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 and len(proc.stdout) == 4096 else None


def detail_changed(a, b):
    """8×8 局部块的平均像素差 >4 灰度级，保护少量新增文字。"""
    if a is None or b is None:
        return True
    for y in range(0, 64, 8):
        for x in range(0, 64, 8):
            diff = sum(abs(a[row * 64 + col] - b[row * 64 + col])
                       for row in range(y, y + 8) for col in range(x, x + 8)) / 64
            if diff > 4:
                return True
    return False


def ahash_from_gray(data):
    """256 字节灰度 → 256 bit aHash（int）：bit = 像素值 > 均值。"""
    if len(data) != GRAY_BYTES:
        raise ValueError(f"aHash 需要 {GRAY_BYTES} 字节灰度，实得 {len(data)}")
    avg = sum(data) / GRAY_BYTES
    h = 0
    for b in data:
        h = (h << 1) | (1 if b > avg else 0)
    return h


def hamming(a, b):
    """两个 256 bit 哈希的汉明距离。"""
    return (a ^ b).bit_count()


def gray_stats(data):
    """256 字节灰度 → (平均亮度, 亮度标准差)。A01 去重辅助判据。"""
    if len(data) != GRAY_BYTES:
        raise ValueError(f"需要 {GRAY_BYTES} 字节灰度，实得 {len(data)}")
    mean = sum(data) / GRAY_BYTES
    var = sum((b - mean) ** 2 for b in data) / GRAY_BYTES
    return mean, var ** 0.5


# A01 亮度辅助判据容差（灰度级 0–255）：纯黑（均值 0）与纯白（均值 255）
# 哈希同为 0，靠亮度差区分开不互剔
BRIGHTNESS_MEAN_TOL = 8.0
BRIGHTNESS_STD_TOL = 8.0


def _entry_t(entry):
    """frames.json 条目的时间：actual_t 缺省回退 t。"""
    t = entry.get("actual_t", entry.get("t"))
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0


def dedup_frames(job, extract_fn=None, detail_fn=None):
    """关键帧去重 postprocess：aHash 相似度 ≥ threshold 的帧标记剔除（不删文件）。

    A01 保守化：剔除需同时满足 aHash 相似度 ≥ 阈值 **且** 平均亮度差 ≤ 8、
    亮度标准差差 ≤ 8（纯黑/纯白哈希同为 0 但亮度差 255，不互剔）；
    关键字补帧（pass_id=kw1）不参与剔除（保护用户明确要的帧）。
    逐帧（按 t 排序）与「上一个保留帧」比较；ffmpeg 抽像素失败的帧记日志保留并
    计入 kept。生成 <run_dir>/dedup_report.json（含容差与 skipped_keyword_frames）；
    result["dedup"] = {total_count, kept_count, dropped_count}。
    extract_fn 可注入（测试用），默认走 ffmpeg。
    """
    if extract_fn is None:
        ffmpeg = common.find_tool("ffmpeg")
        if not ffmpeg:
            job.log("[webui] 未找到 ffmpeg，跳过关键帧去重")
            return
        extract_fn = lambda p: _extract_gray16(ffmpeg, p, job)  # noqa: E731
        detail_fn = lambda p: _extract_gray64(ffmpeg, p, job)  # noqa: E731
    threshold = float(job.dedup.get("threshold", 0.95))
    for r in job.results:
        if not r.get("ok") or not r.get("frames_json"):
            continue
        frames_json = Path(r["frames_json"])
        if not frames_json.is_file():
            job.log(f"[webui] {r.get('run_dir')}: 无 frames.json，跳过去重")
            continue
        try:
            entries = json.loads(frames_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            job.log(f"[webui] frames.json 读取失败，跳过去重: {exc}")
            continue
        if not isinstance(entries, list):
            job.log("[webui] frames.json 顶层不是数组，跳过去重")
            continue
        kept_sig = None          # (hash, mean, std) of 上一个保留帧
        kept_detail = None
        kept = 0
        dropped_list = []
        skipped_kw = 0
        for e in sorted(entries, key=_entry_t):
            if SCHED.is_cancelled(job.id):
                return
            if not isinstance(e, dict) or e.get("dropped") or not e.get("file"):
                continue
            if e.get("pass_id") == KEYWORD_PASS_ID or e.get("keyword_locked"):
                # A01：关键字帧用户明确点名要，不参与去重剔除（计 kept 但不改基准）
                kept += 1
                skipped_kw += 1
                continue
            gray = extract_fn(frames_json.parent / e["file"])
            if gray is None:
                job.log(f"[webui] 抽像素失败，保留: {e['file']}")
                kept += 1            # 哈希读取失败的帧计入 kept（C06）
                continue
            h = ahash_from_gray(gray)
            mean, std = gray_stats(gray)
            detail = detail_fn(frames_json.parent / e["file"]) if detail_fn else None
            if kept_sig is not None:
                kh, km, ks = kept_sig
                similarity = 1 - hamming(h, kh) / GRAY_BYTES
                # A01：相似度达标还要亮度接近才剔除（纯黑纯白哈希同为 0 不互剔）
                if (similarity >= threshold
                        and abs(mean - km) <= BRIGHTNESS_MEAN_TOL
                        and abs(std - ks) <= BRIGHTNESS_STD_TOL
                        and (detail_fn is None or not detail_changed(detail, kept_detail))):
                    e["dropped"] = True
                    e["dedup_similarity"] = round(similarity, 4)
                    dropped_list.append({"file": e["file"], "t": _entry_t(e),
                                         "similarity": round(similarity, 4)})
                    continue
            kept_sig = (h, mean, std)   # 只与「上一个保留帧」比较
            kept_detail = detail
            kept += 1
        if SCHED.is_cancelled(job.id):
            return
        _write_json_atomic(frames_json, entries)
        report = {"threshold": threshold,
                  "mean_tolerance": BRIGHTNESS_MEAN_TOL,
                  "std_tolerance": BRIGHTNESS_STD_TOL,
                  "detail_grid": 64 if detail_fn else None,
                  "detail_block_tolerance": 4 if detail_fn else None,
                  "total": len(entries), "kept": kept, "dropped": dropped_list,
                  "skipped_keyword_frames": skipped_kw}
        _write_json_atomic(Path(r["run_dir"]) / "dedup_report.json", report)
        # C06 统一口径：total_count / kept_count / dropped_count
        r["dedup"] = {"total_count": len(entries), "kept_count": kept,
                      "dropped_count": len(dropped_list)}
        job.log(f"[webui] 关键帧去重：保留 {kept} / 剔除 {len(dropped_list)}"
                f"（阈值 {threshold}；亮度容差 {BRIGHTNESS_MEAN_TOL:g}；"
                f"关键字帧豁免 {skipped_kw}）")


def friendly_outputs(job):
    """为每个成功产物在 run_dir 里生成中文醒目命名的副本。

    - transcript.txt / transcript.srt → 文字稿.txt / 文字稿.srt
    - frames/ 下的帧图 → 关键帧/（C06：只复制保留帧——frames.json 里未标
      dropped 的条目；索引缺失/损坏时回退为全量 *.jpg。原 frames/ 始终全量保留）
    原文件保留不动（AI 工作流与 refine 仍按原名引用）；副本路径写入
    result["friendly"]，供前端优先展示。单个产物失败只记日志。
    """
    for r in job.results:
        if not r.get("ok") or not r.get("run_dir"):
            continue
        run_dir = Path(r["run_dir"])
        friendly = {}
        try:
            tt = r.get("transcript_txt")
            if tt and Path(tt).is_file():
                dst = run_dir / "文字稿.txt"
                shutil.copy2(tt, dst)
                friendly["transcript_txt"] = str(dst)
                srt = Path(tt).with_suffix(".srt")
                if srt.is_file():
                    dst_srt = run_dir / "文字稿.srt"
                    shutil.copy2(srt, dst_srt)
                    friendly["transcript_srt"] = str(dst_srt)
            fd = r.get("frames_dir")
            if fd and Path(fd).is_dir():
                # C06：以 frames.json 索引为准，只复制保留帧；索引不可用回退全量
                kept_names = None
                fj = r.get("frames_json")
                if fj and Path(fj).is_file():
                    try:
                        entries = json.loads(Path(fj).read_text(encoding="utf-8"))
                        if isinstance(entries, list):
                            kept_names = [e["file"] for e in entries
                                          if isinstance(e, dict)
                                          and not e.get("dropped") and e.get("file")]
                    except (OSError, json.JSONDecodeError):
                        kept_names = None
                if kept_names is None:
                    kept_names = [p.name for p in sorted(Path(fd).glob("*.jpg"))]
                dst_dir = run_dir / "关键帧"
                dst_dir.mkdir(exist_ok=True)
                n = 0
                for name in kept_names:
                    src = Path(fd) / name
                    if not src.is_file():
                        continue
                    shutil.copy2(src, dst_dir / src.name)
                    n += 1
                if n:
                    friendly["frames_dir"] = str(dst_dir)
                    friendly["frame_count"] = n
        except OSError as exc:
            job.log(f"[webui] 中文命名副本生成失败（已忽略）: {exc}")
            continue
        if friendly:
            r["friendly"] = friendly
            job.log(f"[webui] 已生成中文命名产物: {run_dir}")


def run_postprocess(job):
    """产物后处理：关键字锁帧 → 关键帧去重 → 中文命名副本 → 预留钩子位。

    顺序保证 friendly_outputs 的 关键帧/ 副本包含关键字新帧、且去重标记已写入
    frames.json。预留钩子后续可挂接流水线之外的其他产物加工；约定：钩子接收
    job、读取/修改 job.results（含 run_dir / frames_dir 等），抛出的异常只记
    日志、不影响任务状态。
    """
    if SCHED.is_cancelled(job.id):
        return
    if job.want_frames and job.keywords:
        try:
            keyword_lock_frames(job)
        except Exception as exc:
            job.log(f"[webui] 关键字锁帧失败: {exc}")
            for r in job.results:
                if r.get("ok"):
                    r["frames_error"] = f"关键字锁帧失败: {exc}"
    if SCHED.is_cancelled(job.id):
        return
    if job.want_frames and job.dedup.get("enabled"):
        try:
            dedup_frames(job)
        except Exception as exc:
            job.log(f"[webui] 关键帧去重失败: {exc}")
            for r in job.results:
                if r.get("ok"):
                    r["frames_error"] = f"关键帧去重失败: {exc}"
    if SCHED.is_cancelled(job.id):
        return
    try:
        friendly_outputs(job)
    except Exception as exc:
        job.log(f"[webui] 中文命名副本异常（已忽略）: {exc}")
    hook = job.postprocess
    if hook is None:
        return
    try:
        hook(job)
    except Exception as exc:
        job.log(f"[webui] 后处理钩子失败（已忽略）: {exc}")


def annotate_artifact_status(job):
    """C02：按产物事实标注 transcript/frames 状态 → result["artifacts"]。

    每个产物 {status: succeeded|skipped|empty|failed, reason?}；依据：
    文件实际存在性、transcript_source（none → 无可转写）、has_video/has_audio
    （C01 下载后 ffprobe 复检的准确值）、frame_count。在后处理完成后调用，
    此时关键字/去重/间隔抽帧已把 frames_json、frame_count 更新到位。
    """
    for r in job.results:
        if not r.get("ok"):
            reason = r.get("error") or "任务失败"
            r["artifacts"] = {
                key: {"status": "failed" if selected else "skipped",
                      "reason": reason if selected else "未选择该产物"}
                for key, selected in (("transcript", job.want_transcript), ("frames", job.want_frames))
            }
            continue
        arts = {}
        # ---- 文字稿 ----
        if not job.want_transcript:
            arts["transcript"] = {"status": "skipped", "reason": "未选择该产物"}
        else:
            tt = r.get("transcript_txt")
            if tt and Path(tt).is_file():
                arts["transcript"] = {"status": "succeeded"}
            elif r.get("has_audio") is False:
                arts["transcript"] = {"status": "skipped", "reason": "无音轨"}
            elif r.get("transcript_source") == "none":
                arts["transcript"] = {"status": "skipped",
                                      "reason": "无音轨或无可解析字幕"}
            else:
                arts["transcript"] = {"status": "failed", "reason": "文字稿文件未生成"}
        # ---- 关键帧 ----
        if not job.want_frames:
            arts["frames"] = {"status": "skipped", "reason": "未选择该产物"}
        else:
            fj = r.get("frames_json")
            count = r.get("frame_count")
            if count is None and fj and Path(fj).is_file():
                try:
                    index = json.loads(Path(fj).read_text(encoding="utf-8"))
                    count = len(index) if isinstance(index, list) else None
                    r["frame_count"] = count
                except (OSError, ValueError):
                    pass
            if r.get("has_video") is False:
                arts["frames"] = {"status": "skipped", "reason": "无视频流"}
            elif r.get("frames_error"):
                arts["frames"] = {"status": "failed", "reason": r["frames_error"]}
            elif fj and Path(fj).is_file() and count:
                arts["frames"] = {"status": "succeeded"}
            elif fj and Path(fj).is_file():
                arts["frames"] = {"status": "empty", "reason": "0 帧"}
            else:
                arts["frames"] = {"status": "failed", "reason": "帧产物未生成"}
        r["artifacts"] = arts


def summarize_artifact_status(job):
    """任务状态汇总已选产物；无音轨等合理跳过不算失败。"""
    states = [r.get("artifacts", {}).get(key, {}).get("status", "failed")
              for r in job.results for key, selected in
              (("transcript", job.want_transcript), ("frames", job.want_frames)) if selected]
    failures = states.count("failed")
    if states and not failures:
        return "done", None
    message = f"已选产物失败 {failures}/{len(states)}（详见结果与日志）"
    if any(state in ("succeeded", "empty") for state in states):
        return "partial", message
    return "error", message if states else "没有生成处理结果（详见日志）"


def _classify_entry_error(entry, tail_text):
    """T04：给失败条目附下载错误分类（type + 中文建议）。"""
    cls = webui_jobs.classify_download_error(
        (entry.get("error") or "") + "\n" + (tail_text or ""))
    if cls:
        entry["download_error"] = cls


def run_job(job):
    """任务执行器：queued →（调度器拿槽位）→ running → 逐 URL 调 watch.py。

    T02：槽位调度 + 取消（排队可取消；运行中杀本任务进程树）；
    T03：同源媒体写锁 + 纯抽帧任务复用已下载媒体；
    T04：下载失败分类 + 连接/无数据类自动重试一次（可取消）。
    """
    # ---- T02 排队拿槽位 ----
    persist_job(job, force=True)                     # queued 落库
    if not SCHED.acquire(job):
        job.finish("cancelled", "排队中取消")
        _finalize_job_progress(job, "cancelled")
        persist_job(job, force=True)
        return
    job.status = "running"
    persist_job(job, force=True)
    # ---- T03 同源媒体写锁（等待可取消，不超时绕过写锁） ----
    lock = media_lock(job.media_key) if job.media_key else None
    if lock:
        while not lock.acquire(timeout=0.2):
            if SCHED.is_cancelled(job.id):
                lock = None
                break
    try:
        for idx, entry in enumerate(job.urls, 1):
            if SCHED.is_cancelled(job.id):
                job.finish("cancelled", "已取消")
                _finalize_job_progress(job, "cancelled")
                return
            if isinstance(entry, dict):   # U06 批量逐条选集 {"url", "item"}
                url, entry_item = entry["url"], entry.get("item")
            else:
                url, entry_item = entry, None
            with job._lock:
                job.current_item = idx
                job.items_done = idx - 1
                for key, selected in (("transcript", job.want_transcript),
                                      ("frames", job.want_frames)):
                    ch = job.progress[key]
                    if selected and ch["state"] == "running":
                        ch["intra"] = 0.0
                        ch["stage"] = f"第 {idx}/{len(job.urls)} 项"
                        ch["percent"] = _weighted_percent(job, ch)
            item = job.item if job.mode == "multi" else entry_item
            # 单个缓存文件不能代表 all/范围选集；逐输入独立缓存，避免批量串片。
            cache_key = webui_jobs.media_key(url, item) if not item or str(item).isdigit() else None
            interval_mode = job.want_frames and job.frame_rule.get("type") == "interval"
            # ---- T03 纯抽帧任务复用同源已下载媒体（转写任务仍需平台字幕，总是下载） ----
            effective_input = url
            reused_from = None
            if not job.want_transcript and cache_key and STORE is not None:
                cached = STORE.media_get(cache_key)
                if cached and Path(cached["video_path"]).is_file():
                    effective_input = cached["video_path"]
                    reused_from = cached["run_dir"]
                    job.log(f"[webui] 复用同源媒体，免重复下载 → {cached['video_path']}")
            cmd = build_watch_command(effective_input, job.want_transcript,
                                      job.want_frames and not interval_mode,
                                      None if reused_from else item, job.frame_rule,
                                      None if job.frame_width == 512 else job.frame_width)
            if len(job.urls) > 1:
                job.log(f"═══ 批量进度 {idx}/{len(job.urls)} ═══")
            job.log(f"$ watch.py {common.redact_url(url)}"
                    f"{' --item ' + str(item) if item else ''} --no-review")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"   # 保证子进程输出 UTF-8
            env["VIDEO_WATCH_RUNS_DIR"] = str(RUNS_ROOT)
            # ---- T04 失败分类可重试的下载错误自动重试一次 ----
            result, tail_text = None, ""
            for attempt in (1, 2):
                if SCHED.is_cancelled(job.id):
                    break
                result, tail_text = _run_watch_attempt(job, cmd, env)
                if result is not None and result.get("ok"):
                    break
                probe_err = webui_jobs.classify_download_error(
                    ((result or {}).get("error") or "") + "\n" + tail_text)
                if attempt == 1 and webui_jobs.is_retryable(probe_err):
                    job.log(f"[webui] 下载失败（{probe_err['type']}），自动重试一次…")
                    continue
                break
            if SCHED.is_cancelled(job.id):
                job.finish("cancelled", "已取消")
                _finalize_job_progress(job, "cancelled")
                return
            persist_job(job)
            if result is None:
                entry_out = {"url": common.redact_url(url), "ok": False,
                             "error": f"watch.py 未输出 RESULT_JSON"}
                _classify_entry_error(entry_out, tail_text)
                job.add_results([entry_out])
                continue
            entries = extract_results(result, url)
            for e in entries:
                if not e.get("ok"):
                    _classify_entry_error(e, tail_text)
                elif reused_from:
                    e["reused_from"] = reused_from
            if isinstance(result.get("episodes"), list):
                # C05：多集总数以聚合 RESULT 为准（all 模式探测后才可知）
                with job._lock:
                    job.items_total = result.get("total") or job.items_total
            if interval_mode:
                for e in entries:
                    if e.get("ok") and e.get("run_dir"):
                        run_interval_frames(job, e)
            # T03：成功产物回填媒体缓存 + manifest 记 reused_from（版本记录）
            if cache_key and STORE is not None and len(entries) == 1:
                _record_media_cache(cache_key, entries, reused_from)
            job.add_results(entries)
            persist_job(job)
        # C05：后处理期间 frames 通道显示中间态，100% 留给全部步骤结束
        if job.frames_postprocess_pending():
            with job._lock:
                fr = job.progress["frames"]
                if fr["state"] == "running":
                    fr["stage"] = "整理结果…"
        if SCHED.is_cancelled(job.id):
            job.finish("cancelled", "已取消")
            _finalize_job_progress(job, "cancelled")
            return
        run_postprocess(job)
        if SCHED.is_cancelled(job.id):
            job.finish("cancelled", "已取消")
            _finalize_job_progress(job, "cancelled")
            return
        annotate_artifact_status(job)
        status, err = summarize_artifact_status(job)
        job.finish(status, err)
        _finalize_job_progress(job, status)
    except Exception as exc:  # 兜底：任务线程异常不拖垮 HTTP 服务
        job.log(f"[webui] 任务执行器异常: {exc}")
        status = "cancelled" if SCHED.is_cancelled(job.id) else "error"
        job.finish(status, "已取消" if status == "cancelled" else str(exc))
        _finalize_job_progress(job, status)
    finally:
        if lock:
            lock.release()
        SCHED.release(job)
        persist_job(job, force=True)


def _run_watch_attempt(job, cmd, env):
    """跑一次 watch.py 子进程：Popen 注册到调度器（可取消杀树），流式读日志。

    返回 (RESULT_JSON dict 或 None, 尾部输出文本)。
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 错误细节并入日志流；RESULT_JSON 倒序查找不受影响
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        job.log(f"[webui] 子进程启动失败: {exc}")
        return None, str(exc)
    SCHED.register_proc(job.id, proc)   # T02：取消时 taskkill 这棵树
    tail = deque(maxlen=PARSE_TAIL_LIMIT)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:            # 实时逐行读，追加到日志缓冲并驱动进度条
            line = line.rstrip("\r\n")
            tail.append(line)
            job.log(line)
            update_progress(job, line)
        proc.wait()
    except BaseException:
        webui_jobs._kill_tree(proc)
        proc.wait()
        raise
    finally:
        SCHED.register_proc(job.id, None)
    return parse_result_json("\n".join(tail)), "\n".join(tail)


def _record_media_cache(cache_key, entries, reused_from):
    """T03：成功 URL 产物回填媒体缓存；复用任务在 manifest 记 reused_from。"""
    for e in entries:
        if not e.get("ok") or not e.get("run_dir"):
            continue
        video_path = None
        try:
            manifest = json.loads(
                (Path(e["run_dir"]) / "manifest.json").read_text(encoding="utf-8"))
            video_path = manifest.get("video_path")
        except (OSError, json.JSONDecodeError):
            pass
        if video_path and Path(video_path).is_file():
            STORE.media_set(cache_key, e["run_dir"], video_path)
        if reused_from:
            # 版本记录：本次产物来自复用媒体
            mp = Path(e["run_dir"]) / "manifest.json"
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
                manifest["reused_from"] = reused_from
                _write_json_atomic(mp, manifest)
            except (OSError, json.JSONDecodeError):
                pass


# ---------------------------------------------------------------- 环境自检


def detect_gpu():
    """尽力探测 NVIDIA GPU（调用 nvidia-smi）；任何失败都不致命。"""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        names = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
        if proc.returncode == 0 and names:
            return {"available": True, "devices": names}
    except Exception:
        pass
    return {"available": False, "devices": [],
            "hint": "未检测到 NVIDIA GPU（或 nvidia-smi 不可用），转写将使用 CPU"}


def _cuda_capability():
    """U09：CUDA 运行库与可见设备（尽力而为；任何失败记 None=未知，不误导）。"""
    libs = []
    for dist in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12"):
        try:
            importlib.metadata.version(dist)
            libs.append(dist)
        except Exception:
            pass
    devices = None
    try:
        import glob as _glob
        import site as _site
        for base in set(_site.getsitepackages() + [_site.getusersitepackages()]):
            for d in _glob.glob(os.path.join(base, "nvidia", "*", "bin")):
                if os.path.isdir(d):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        import ctranslate2  # noqa
        devices = ctranslate2.get_cuda_device_count()
    except Exception:
        devices = None
    return {"libs_installed": libs, "cuda_devices": devices}


def _model_cache():
    """常用 whisper 模型缓存情况（HF 标准缓存目录，尽力而为；失败 None=未知）。"""
    try:
        cache = Path(os.environ.get(
            "HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
        return [size for size in ("tiny", "small", "medium")
                if any(cache.glob(f"models--Systran--faster-whisper-{size}"))]
    except Exception:
        return None


def build_health():
    """环境自检 + U09 能力视图：抽帧/转写/GPU/CUDA库/模型缓存分项呈现。"""
    tools = {name: common.find_tool(name) for name in ("ffmpeg", "ffprobe", "yt-dlp")}
    packages = {}
    for mod in ("yt_dlp", "faster_whisper"):
        try:
            packages[mod] = importlib.util.find_spec(mod) is not None
        except Exception:
            packages[mod] = False
    missing = [name for name, path in tools.items() if not path]
    missing += [f"pip:{mod}" for mod, ok in packages.items() if not ok]
    gpu = detect_gpu()
    cuda = _cuda_capability()
    capabilities = {
        "frames_ready": bool(tools["ffmpeg"] and tools["ffprobe"]),   # 纯抽帧不依赖转写包
        "transcribe_ready": bool(packages["faster_whisper"]),
        "gpu_detected": gpu["available"],
        "gpu_devices": gpu["devices"],
        "cuda_libs_installed": cuda["libs_installed"],
        "cuda_devices": cuda["cuda_devices"],     # None = 未知
        "models_cached": _model_cache(),          # None = 未知
    }
    return {"ok": True, "tools": tools, "packages": packages,
            "missing": missing, "gpu": gpu, "capabilities": capabilities}


# ---------------------------------------------------------------- 请求校验与路径安全


_KEYWORD_SPLIT_RE = re.compile(r"[\s,，、]+")   # 关键字分隔：空格/中英文逗号/顿号
MAX_KEYWORD_LEN = 100
DEDUP_THRESHOLD_MIN, DEDUP_THRESHOLD_MAX = 0.80, 0.99


def _validate_item_spec(item):
    """选集表达式 '3' / '3-7' / 'all' 校验 → (True, None) 或 (False, 错误消息)。"""
    if item == "all":
        return True, None
    m = _ITEM_SPEC_RE.fullmatch(item)
    if not m:
        return False, f"集数范围无效: {item!r}（支持 3 / 3-7 / all）"
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) is not None else first
    if first < 1:
        return False, "集数从 1 起"
    if last < first:
        return False, "区间终点不能小于起点"
    return True, None


def parse_keywords(text):
    """关键字串 → 列表：空格/逗号/顿号分隔，去空去重保序。"""
    out = []
    for part in _KEYWORD_SPLIT_RE.split(str(text)):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def validate_job_request(payload):
    """校验 POST /api/jobs 请求体 → (params, None) 或 (None, 错误消息)。"""
    if not isinstance(payload, dict):
        return None, "请求体必须是 JSON 对象"
    mode = payload.get("mode")
    if mode not in ("single", "multi", "batch"):
        return None, "mode 必须是 single / multi / batch"
    # T05 严格类型：bool 字段必须是 bool，不接受真值强制转换
    for bkey in ("want_transcript", "want_frames"):
        if bkey in payload and not isinstance(payload[bkey], bool):
            return None, f"{bkey} 必须是布尔值"
    want_transcript = bool(payload.get("want_transcript"))
    want_frames = bool(payload.get("want_frames"))
    if not want_transcript and not want_frames:
        return None, "请至少选择一种产物（文字稿 / 关键帧）"
    if "url" in payload and payload["url"] is not None and not isinstance(payload["url"], str):
        return None, "url 必须是字符串"
    if mode == "batch":
        raw_urls = payload.get("urls")
        if not isinstance(raw_urls, list):
            return None, "batch 模式需要 urls 列表"
        urls = []
        for entry in raw_urls:
            if isinstance(entry, str):
                u = entry.strip()
                if u:
                    urls.append(u)
            elif isinstance(entry, dict):
                # U06：批量条目可带逐条选集 {"url": ..., "item": "3|3-7|all"}
                if not isinstance(entry.get("url"), str):
                    return None, "urls 条目的 url 必须是字符串"
                if entry.get("item") is not None and not isinstance(entry.get("item"), str):
                    return None, "urls 条目的 item 必须是字符串"
                u = entry["url"].strip()
                it = (entry.get("item") or "").strip().lower() or None
                if it:
                    ok_it, it_err = _validate_item_spec(it)
                    if not ok_it:
                        return None, it_err
                if u:
                    urls.append({"url": u, "item": it})
            else:
                return None, "urls 元素必须是字符串或 {url, item} 对象"
        if not urls:
            return None, "urls 不能为空"
    else:
        url = str(payload.get("url") or "").strip()
        if not url:
            return None, "URL 不能为空"
        urls = [url]
    item = None
    if mode == "multi":
        raw_item = payload.get("item")
        if raw_item is not None and not isinstance(raw_item, str):
            return None, "item 必须是字符串"
        item = str(raw_item or "").strip().lower() or "all"
        ok_it, it_err = _validate_item_spec(item)
        if not ok_it:
            return None, it_err

    # 长度/类型限制仍覆盖整个请求；不因产物开关放宽输入边界。
    raw_keyword = payload.get("keyword")
    if raw_keyword is not None and not isinstance(raw_keyword, str):
        return None, "keyword 必须是字符串"
    if raw_keyword and len(raw_keyword.strip()) > MAX_KEYWORD_LEN:
        return None, f"关键字过长（≤{MAX_KEYWORD_LEN} 字符）"
    # 只生成文字稿时，侧栏的帧设置不参与校验或后处理。
    if not want_frames:
        payload = dict(payload, frame_rule=None, keyword=None, dedup=None, frame_width=None)

    # ---- 关键帧截取原则：default / count(1–100) / interval(0.5–600 秒) ----
    raw_rule = payload.get("frame_rule")
    if raw_rule is None:
        frame_rule = {"type": "default"}
    elif not isinstance(raw_rule, dict):
        return None, "frame_rule 必须是对象"
    else:
        ftype = raw_rule.get("type", "default")
        if ftype == "default":
            frame_rule = {"type": "default"}
        elif ftype == "count":
            value = raw_rule.get("value")
            # 严格整数：拒绝 bool 与小数（1.8 不允许 int() 截断，C03）
            if isinstance(value, bool):
                return None, "自定义帧数需为 1–100 的整数"
            if isinstance(value, float) and not value.is_integer():
                return None, "自定义帧数需为 1–100 的整数（不接受小数）"
            try:
                n = int(value)
            except (TypeError, ValueError):
                return None, "自定义帧数需为 1–100 的整数"
            if not (1 <= n <= 100):
                return None, "自定义帧数需为 1–100 的整数"
            frame_rule = {"type": "count", "value": n}
        elif ftype == "interval":
            value = raw_rule.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return None, "自定义间隔需为 0.5–600 秒"
            try:
                x = float(value)
            except (TypeError, ValueError):
                return None, "自定义间隔需为 0.5–600 秒"
            if not (0.5 <= x <= 600):
                return None, "自定义间隔需为 0.5–600 秒"
            frame_rule = {"type": "interval", "value": x}
        else:
            return None, f"未知 frame_rule.type: {ftype!r}"

    # ---- 关键字定位关键帧（依赖文字稿：带关键字时自动补选文字稿） ----
    raw_kw = payload.get("keyword")
    if raw_kw is not None and not isinstance(raw_kw, str):
        return None, "keyword 必须是字符串"
    raw_keyword = str(raw_kw or "").strip()
    if len(raw_keyword) > MAX_KEYWORD_LEN:
        return None, f"关键字过长（≤{MAX_KEYWORD_LEN} 字符）"
    keywords = parse_keywords(raw_keyword)
    if keywords and not want_transcript:
        want_transcript = True

    # ---- 关键帧去重 ----
    raw_dedup = payload.get("dedup")
    if raw_dedup is None:
        raw_dedup = {}
    if not isinstance(raw_dedup, dict):
        return None, "dedup 必须是对象"
    if "enabled" in raw_dedup and not isinstance(raw_dedup["enabled"], bool):
        return None, "dedup.enabled 必须是布尔值"
    threshold_raw = raw_dedup.get("threshold", 0.95)
    if isinstance(threshold_raw, bool):
        return None, f"相似度阈值需在 {DEDUP_THRESHOLD_MIN}–{DEDUP_THRESHOLD_MAX} 之间"
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        return None, f"相似度阈值需在 {DEDUP_THRESHOLD_MIN}–{DEDUP_THRESHOLD_MAX} 之间"
    if not (DEDUP_THRESHOLD_MIN <= threshold <= DEDUP_THRESHOLD_MAX):
        return None, f"相似度阈值需在 {DEDUP_THRESHOLD_MIN}–{DEDUP_THRESHOLD_MAX} 之间"
    dedup = {"enabled": bool(raw_dedup.get("enabled")), "threshold": threshold}

    # ---- 抽帧宽度（U12：512 默认 / 768 / 1024） ----
    raw_width = payload.get("frame_width")
    frame_width = 512
    if raw_width is not None:
        if isinstance(raw_width, bool) or not isinstance(raw_width, int):
            return None, "frame_width 必须是整数"
        if raw_width not in (512, 768, 1024):
            return None, "frame_width 只支持 512 / 768 / 1024"
        frame_width = raw_width

    return {"mode": mode, "urls": urls, "want_transcript": want_transcript,
            "want_frames": want_frames, "item": item, "frame_rule": frame_rule,
            "keywords": keywords, "dedup": dedup, "frame_width": frame_width}, None


def resolve_runs_path(raw):
    """把用户给的 path 解析为 RUNS_ROOT 内的绝对路径；越界/非法返回 None。"""
    try:
        p = Path(raw).resolve()
    except Exception:
        return None
    root = RUNS_ROOT
    if p != root and root not in p.parents:
        return None
    return p


def run_export_pdf(run_dir, per_page=6):
    """同步调 make_frames_pdf.py 导出关键帧 PDF（百帧 <1s，不必走 Job）。

    per_page ∈ {1,2,6} 透传 --per-page（U12 版式）。
    返回该脚本的 RESULT_JSON dict：{ok, pdf, pages, frames_included, ...}
    或 {ok: False, error}；子进程 list 形式、禁 shell、超时 120s。
    """
    cmd = [sys.executable, str(SCRIPTS_DIR / "make_frames_pdf.py"),
           "--run-dir", str(run_dir), "--per-page", str(per_page)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"   # 保证子进程输出 UTF-8
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "导出超时（>120s）"}
    except OSError as exc:
        return {"ok": False, "error": f"导出子进程启动失败: {exc}"}
    result = parse_result_json(proc.stdout or "")
    if result is None:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-300:]
        return {"ok": False,
                "error": f"make_frames_pdf.py 未输出 RESULT_JSON（退出码 {proc.returncode}）: {tail}"}
    return result


def dedup_restore(run_dir, file_name):
    # 同一索引的并发恢复串行，防止互相覆盖原子写临时文件。
    with media_lock("restore:" + str(Path(run_dir).resolve())):
        return _dedup_restore_locked(run_dir, file_name)


def _dedup_restore_locked(run_dir, file_name):
    """U08：恢复某张被去重隐藏的帧（幂等）。

    去掉 frames.json 里该帧 dropped/dedup_similarity 标记（原子写），同步
    dedup_report.json 统计；若 关键帧/ 副本目录存在则补拷该帧（口径一致）。
    返回 {ok, total_count, kept_count, dropped_count} 或 {ok: False, error}。
    """
    frames_json = Path(run_dir) / "frames" / "frames.json"
    if not frames_json.is_file():
        return {"ok": False, "error": "frames.json 不存在"}
    try:
        entries = json.loads(frames_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"frames.json 读取失败: {exc}"}
    if not isinstance(entries, list):
        return {"ok": False, "error": "frames.json 顶层不是数组"}
    target = next((e for e in entries
                   if isinstance(e, dict) and e.get("file") == file_name), None)
    if target is None:
        return {"ok": False, "error": f"帧不在索引中: {file_name}"}
    if target.get("dropped"):
        target.pop("dropped", None)
        target.pop("dedup_similarity", None)
        _write_json_atomic(frames_json, entries)
    dropped_now = [e for e in entries if isinstance(e, dict) and e.get("dropped")]
    # 同步 dedup_report.json（移除该帧、重算 kept）
    report_path = Path(run_dir) / "dedup_report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["dropped"] = [d for d in (report.get("dropped") or [])
                                 if isinstance(d, dict) and d.get("file") != file_name]
            report["kept"] = len(entries) - len(dropped_now)
            _write_json_atomic(report_path, report)
        except (OSError, json.JSONDecodeError):
            pass
    # 关键帧/ 副本目录补拷（按钮/预览/PDF/副本口径一致）
    copy_dir = Path(run_dir) / "关键帧"
    src = frames_json.parent / file_name
    if copy_dir.is_dir() and src.is_file():
        try:
            shutil.copy2(src, copy_dir / src.name)
        except OSError:
            pass
    return {"ok": True, "total_count": len(entries),
            "kept_count": len(entries) - len(dropped_now),
            "dropped_count": len(dropped_now)}


def run_probe(input_value):
    """U06 预检：调 probe.py 只探测不下载（60s 超时；失败传播 ok:false）。

    返回 probe.py 的 RESULT_JSON dict：{ok, title, duration, has_audio,
    has_video, playlist{count, items[{index,title,duration}]}, ...}；
    probe.py 自身已对 URL 脱敏。
    """
    cmd = [sys.executable, str(SCRIPTS_DIR / "probe.py"),
           "--input", str(input_value)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "探测超时（>60s）"}
    except OSError as exc:
        return {"ok": False, "error": f"探测子进程启动失败: {exc}"}
    result = parse_result_json(proc.stdout or "")
    if result is None:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-300:]
        return {"ok": False,
                "error": f"probe.py 未输出 RESULT_JSON（退出码 {proc.returncode}）: {tail}"}
    return result


# ---------------------------------------------------------------- HTTP 层


class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "VideoWatchWebUI/1.0"

    def log_message(self, fmt, *args):  # 1s 轮询下默认访问日志太吵，静默
        pass

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", status)

    # ---- GET ----

    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path == "/":
                self._serve_index()
            elif path == "/api/health":
                self._send_json(build_health())
            elif path == "/api/instance":
                self._send_json({"application": "video-watch-webui", "api_version": 1,
                                 "project_root": str(SKILL_ROOT.resolve()), "runs_root": str(RUNS_ROOT)})
            elif path == "/api/jobs":
                self._list_jobs(parsed.query)
            elif path.startswith("/api/jobs/"):
                self._serve_job(path.rsplit("/", 1)[-1])
            elif path == "/api/files":
                self._serve_files(parsed.query)
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # 单个请求异常不影响服务
            try:
                self._send_json({"ok": False, "error": f"服务器内部错误: {exc}"}, 500)
            except Exception:
                pass

    def _serve_index(self):
        try:
            body = INDEX_HTML.read_bytes()
        except OSError:
            self._send_json({"ok": False, "error": "webui/index.html 不存在"}, 500)
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _serve_job(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is not None:
            self._send_json(job.snapshot())
            return
        # T01：内存 miss 时查库（服务重启后历史任务仍可见，running 已标 interrupted）
        if STORE is not None:
            row = STORE.get(job_id)
            if row is not None:
                self._send_json(db_row_snapshot(row))
                return
        self._send_json({"ok": False, "error": "任务不存在"}, 404)

    def _list_jobs(self, query):
        """GET /api/jobs：任务列表（倒序，limit/offset 分页；内存活动任务覆盖库快照）。"""
        qs = urllib.parse.parse_qs(query)
        try:
            limit = min(50, max(1, int(qs.get("limit", ["20"])[0])))
            offset = max(0, int(qs.get("offset", ["0"])[0]))
        except (ValueError, TypeError):
            self._send_json({"ok": False, "error": "limit/offset 必须是整数"}, 400)
            return
        rows = STORE.list(limit, offset) if STORE is not None else []
        items = []
        for row in rows:
            with JOBS_LOCK:
                j = JOBS.get(row["job_id"])
            items.append(j.snapshot() if j is not None else db_row_snapshot(row))
        seen = {row["job_id"] for row in rows}
        with JOBS_LOCK:   # 内存中刚创建还没落库的（竞态极小）
            fresh = [j.snapshot() for jid, j in JOBS.items() if jid not in seen]
        self._send_json({"ok": True, "jobs": fresh + items,
                         "limit": limit, "offset": offset})

    _STATIC_TYPES = {".css": "text/css; charset=utf-8",
                     ".js": "text/javascript; charset=utf-8",
                     ".png": "image/png", ".jpg": "image/jpeg",
                     ".svg": "image/svg+xml", ".ico": "image/x-icon"}

    def _serve_static(self, name):
        """T06：静态资源路由——只允许 webui/ 目录内的白名单文件（防路径穿越）。"""
        p = (STATIC_DIR / name).resolve()
        if (p.parent != STATIC_DIR.resolve() or not p.is_file()
                or p.suffix.lower() not in self._STATIC_TYPES):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        try:
            self._send_bytes(p.read_bytes(), self._STATIC_TYPES[p.suffix.lower()])
        except OSError as exc:
            self._send_json({"ok": False, "error": f"读取失败: {exc}"}, 500)

    def _serve_files(self, query):
        raw = (urllib.parse.parse_qs(query).get("path") or [""])[0]
        if not raw:
            self._send_json({"ok": False, "error": "缺少 path 参数"}, 400)
            return
        p = resolve_runs_path(raw)
        if p is None:
            self._send_json({"ok": False, "error": "路径越界：仅允许访问 runs/ 目录内文件"}, 403)
            return
        if p.is_dir():
            # 目录 → 返回文件清单（供「浏览帧图」网格加载）
            files = [{"name": f.name, "size": f.stat().st_size}
                     for f in sorted(p.iterdir()) if f.is_file()]
            self._send_json({"ok": True, "dir": str(p), "files": files})
            return
        if not p.is_file():
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        ext = p.suffix.lower()
        if ext in _IMAGE_TYPES:
            ctype = _IMAGE_TYPES[ext]
        elif ext == ".json":
            ctype = "application/json; charset=utf-8"
        elif ext == ".pdf":
            ctype = "application/pdf"        # 浏览器内联打开
        elif ext in _TEXT_EXTS:
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        try:
            body = p.read_bytes()
        except OSError as exc:
            self._send_json({"ok": False, "error": f"读取失败: {exc}"}, 500)
            return
        self._send_bytes(body, ctype)

    # ---- POST ----

    def _read_json_body(self):
        """读取并解析 JSON 请求体 → (payload, None) 或 (None, 错误消息)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None, "请求体为空或过大"
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "请求体不是合法 JSON"
        if not isinstance(payload, dict):
            return None, "请求体必须是 JSON 对象"
        return payload, None

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/jobs":
            self._create_job()
        elif parsed.path == "/api/export_pdf":
            self._export_pdf()
        elif parsed.path == "/api/probe":
            self._probe_media()
        elif parsed.path == "/api/check_path":
            self._check_path()
        elif parsed.path == "/api/dedup_restore":
            self._dedup_restore()
        elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
            self._cancel_job(parsed.path[len("/api/jobs/"):-len("/cancel")])
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def _create_job(self):
        payload, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        params, err = validate_job_request(payload)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        job = Job(**params)
        with JOBS_LOCK:
            JOBS[job.id] = job
        persist_job(job, force=True)   # T01：queued 立即落库
        threading.Thread(target=run_job, args=(job,), daemon=True,
                         name=f"webui-job-{job.id}").start()
        self._send_json({"ok": True, "job_id": job.id})

    def _cancel_job(self, job_id):
        """POST /api/jobs/<id>/cancel：T02 排队直接取消；运行中杀本任务进程树。

        顺序：先在锁内把状态置 cancelling（防与 run_job 的 cancelled 收尾竞争），
        再 request_cancel 杀进程树；worker 的 finish("cancelled") 是最终状态。
        """
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self._send_json({"ok": False, "error": "任务不存在或已结束"}, 404)
            return
        with job._lock:
            if job.status in ("done", "partial", "error", "cancelled", "interrupted"):
                self._send_json({"ok": False, "error": f"任务已结束（{job.status}）"}, 409)
                return
            job.status = "cancelling"
        SCHED.request_cancel(job_id)
        persist_job(job, force=True)
        self._send_json({"ok": True, "status": "cancelling"})

    def _dedup_restore(self):
        """POST /api/dedup_restore {run_dir, file}：U08 恢复被去重隐藏的帧。"""
        payload, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        run_dir = resolve_runs_path(str(payload.get("run_dir") or "").strip())
        if run_dir is None:
            self._send_json({"ok": False, "error": "路径越界：仅允许 runs/ 目录内"}, 403)
            return
        if not run_dir.is_dir():
            self._send_json({"ok": False, "error": "任务目录不存在"}, 404)
            return
        file_name = str(payload.get("file") or "").strip()
        if (not file_name or "/" in file_name or "\\" in file_name
                or file_name.startswith(".")):
            self._send_json({"ok": False, "error": "非法帧文件名"}, 400)
            return
        result = dedup_restore(run_dir, file_name)
        self._send_json(result, 200 if result.get("ok") else 500)

    def _export_pdf(self):
        """POST /api/export_pdf {run_dir}：把该任务的关键帧导出为 关键帧.pdf。"""
        payload, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        run_dir = resolve_runs_path(str(payload.get("run_dir") or "").strip())
        if run_dir is None:
            self._send_json({"ok": False, "error": "路径越界：仅允许 runs/ 目录内"}, 403)
            return
        if not run_dir.is_dir():
            self._send_json({"ok": False, "error": "任务目录不存在"}, 404)
            return
        per_page = payload.get("per_page", 6)
        if type(per_page) is not int or per_page not in (1, 2, 6):
            self._send_json({"ok": False, "error": "per_page 必须是 1 / 2 / 6"}, 400)
            return
        result = run_export_pdf(run_dir, per_page)
        self._send_json(result, 200 if result.get("ok") else 500)

    def _probe_media(self):
        """POST /api/probe {input}：U06 预检（只探测不下载）。"""
        payload, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        value = str(payload.get("input") or "").strip().strip('"').strip("'")
        if not value:
            self._send_json({"ok": False, "error": "input 不能为空"}, 400)
            return
        result = run_probe(value)
        self._send_json(result, 200 if result.get("ok") else 500)

    def _check_path(self):
        """POST /api/check_path {path}：U05 本地路径轻量存在性提示（清理引号）。"""
        payload, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        raw = str(payload.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            self._send_json({"ok": False, "error": "path 不能为空"}, 400)
            return
        try:
            p = Path(raw)
            if p.is_file():
                kind = "file"
            elif p.is_dir():
                kind = "dir"
            else:
                kind = None
        except OSError:
            kind = None
        self._send_json({"ok": True, "exists": kind is not None, "kind": kind})


# ---------------------------------------------------------------- 入口


class WebUIServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 可能允许第二个进程绑定同一监听地址。
    # 独占绑定才能可靠识别端口占用与复用现有实例。
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main(argv=None):
    common.setup_stdio()
    ap = argparse.ArgumentParser(
        prog="webui.py",
        description="video-watch 本地 Web 界面（仅监听 127.0.0.1，本机访问）",
    )
    ap.add_argument("--port", type=int, default=8765,
                    help="监听端口（默认 8765；0 = 自动分配空闲端口）")
    ap.add_argument("--no-browser", action="store_true",
                    help="启动后不自动打开浏览器")
    ap.add_argument("--runs-dir", help="产物/历史隔离目录（默认项目 runs/，也可用 VIDEO_WATCH_RUNS_DIR）")
    args = ap.parse_args(argv)
    if not (0 <= args.port <= 65535):
        ap.error("--port 必须在 0-65535 之间")
    global RUNS_ROOT
    if args.runs_dir:
        RUNS_ROOT = Path(args.runs_dir).expanduser().resolve()
    else:
        RUNS_ROOT = common.runs_dir().resolve()
    os.environ["VIDEO_WATCH_RUNS_DIR"] = str(RUNS_ROOT)

    # 必须先绑定成功，再更改历史任务状态；重复启动不能干扰在跑的实例。
    try:
        server = WebUIServer(("127.0.0.1", args.port), WebUIHandler)
    except OSError as exc:
        if exc.errno in (48, 98, 10048) or getattr(exc, "winerror", None) == 10048:
            url = f"http://127.0.0.1:{args.port}/"
            try:
                # 禁用代理，端口识别只访问本机。
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url + "api/instance", timeout=2) as response:
                    info = json.loads(response.read(4096).decode("utf-8"))
                same = (info.get("application") == "video-watch-webui"
                        and info.get("api_version") == 1
                        and Path(info.get("project_root", "")).resolve() == SKILL_ROOT.resolve()
                        and Path(info.get("runs_root", str(common.skill_root() / "runs"))).resolve() == RUNS_ROOT)
            except (OSError, ValueError, TypeError):
                same = False
            if same:
                print(f"[webui] 已有本项目实例运行，复用: {url}", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            print(f"[webui][端口占用] 端口 {args.port} 被其他服务或其他项目占用。"
                  "请使用 --port 9000 或 --port 0 自动分配空闲端口。", file=sys.stderr)
            return 4
        raise

    # T01：任务持久化落库；服务重启后把库里无法续跑的 running/queued 标为 interrupted
    global STORE
    try:
        STORE = webui_jobs.JobStore(RUNS_ROOT / webui_jobs.DB_NAME)
        interrupted = STORE.mark_interrupted()
    except Exception:
        server.server_close()
        raise
    if interrupted:
        print(f"[webui] 已将 {interrupted} 个未完成的历史任务标记为 interrupted", flush=True)

    port = server.server_address[1]   # --port 0 时取实际分配到的端口
    url = f"http://127.0.0.1:{port}/"
    print(f"[webui] 已启动: {url} （Ctrl+C 停止）", flush=True)
    if not args.no_browser:
        # ThreadingHTTPServer 构造返回时端口已完成绑定，此刻打开浏览器不会打到未就绪服务
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] 已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
