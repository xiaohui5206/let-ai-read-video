# -*- coding: utf-8 -*-
"""webui_jobs.py — Web UI 任务持久化（sqlite3）+ 调度（排队/取消/媒体键/错误分类）。

只被 scripts/webui.py 导入使用（同目录，靠 webui.py 的 sys.path 注入），
不参与 CLI 脚本的独立分发设计。

- JobStore：jobs 表（job_id/mode/params/status/progress/results/logs/error/
  media_key/created_at/finished_at）+ media_cache 表（同源媒体键 → run_dir/video_path）。
  标准库 sqlite3，WAL 模式；DB 文件默认 <skill>/runs/.webui_jobs.db（被 .gitignore 的
  runs/ 覆盖）。
- Scheduler：重型任务（含转写）全局并发 1、轻型（纯抽帧）并发 2；queued/running/
  cancelling/cancelled 状态机；取消时用 taskkill /T /F 杀本任务创建的整棵进程树。
- media_key：同源媒体缓存键（T03）；classify_download_error：下载失败分类（T04）。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DB_NAME = ".webui_jobs.db"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- sqlite 持久化


class JobStore:
    """任务与媒体缓存的 sqlite 持久化。线程安全（每次调用独立连接 + 写锁）。"""

    def __init__(self, db_path):
        self._db_path = str(db_path)
        self._wlock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " job_id TEXT PRIMARY KEY, mode TEXT, params TEXT, status TEXT,"
                " progress TEXT, results TEXT, logs TEXT, error TEXT,"
                " media_key TEXT, created_at TEXT, finished_at TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS media_cache ("
                " media_key TEXT PRIMARY KEY, run_dir TEXT, video_path TEXT,"
                " updated_at TEXT)")

    def _open(self):
        """每次调用独立连接并保证关闭（sqlite 的 with 只管事务不管关闭）。"""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return contextlib.closing(conn)

    # ---- jobs 表 ----

    def upsert(self, rec):
        """rec: {job_id, mode, params, status, progress, results, logs, error,
        media_key, created_at, finished_at}（JSON 字段已是 dumps 后的 str）。"""
        with self._wlock, self._open() as conn, conn:
            conn.execute(
                "INSERT INTO jobs (job_id, mode, params, status, progress, results,"
                " logs, error, media_key, created_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " mode=excluded.mode, params=excluded.params, status=excluded.status,"
                " progress=excluded.progress, results=excluded.results,"
                " logs=excluded.logs, error=excluded.error,"
                " media_key=excluded.media_key, created_at=excluded.created_at,"
                " finished_at=excluded.finished_at",
                (rec["job_id"], rec.get("mode"), rec.get("params"), rec.get("status"),
                 rec.get("progress"), rec.get("results"), rec.get("logs"),
                 rec.get("error"), rec.get("media_key"), rec.get("created_at"),
                 rec.get("finished_at")))

    def get(self, job_id):
        with self._open() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, limit=20, offset=0):
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC"
                " LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def mark_interrupted(self):
        """服务启动时调用：库里的 queued/running/cancelling 都是无法续跑的，诚实标记。"""
        with self._wlock, self._open() as conn, conn:
            cur = conn.execute(
                "UPDATE jobs SET status='interrupted', finished_at=?"
                " WHERE status IN ('queued','running','cancelling')",
                (now_iso(),))
            return cur.rowcount

    @staticmethod
    def _row_to_dict(row):
        d = dict(row)
        for key in ("params", "progress", "results", "logs"):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else None
            except (TypeError, json.JSONDecodeError):
                d[key] = None
        return d

    # ---- media_cache 表 ----

    def media_get(self, key):
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM media_cache WHERE media_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def media_set(self, key, run_dir, video_path):
        with self._wlock, self._open() as conn, conn:
            conn.execute(
                "INSERT INTO media_cache (media_key, run_dir, video_path, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(media_key) DO UPDATE SET"
                " run_dir=excluded.run_dir, video_path=excluded.video_path,"
                " updated_at=excluded.updated_at",
                (key, run_dir, video_path, now_iso()))


# ---------------------------------------------------------------- 调度器（T02）


def _kill_tree(proc):
    """杀本任务创建的整棵进程树。

    Windows：taskkill /PID <pid> /T /F —— 树从我们的 Popen 根（python watch.py）起，
    其下 python 子孙（frames.py 等）与 ffmpeg/whisper 全部结束；只作用于本任务
    创建的根 PID，不影响其他进程或用户自己的 ffmpeg。非 Windows 用 kill 根进程。
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            proc.kill()
        except OSError:
            pass


class Scheduler:
    """槽位调度：重型（含转写）并发 1、轻型（纯抽帧）并发 2；支持排队位置与取消。"""

    def __init__(self, heavy_limit=1, light_limit=2):
        self._heavy_limit = heavy_limit
        self._light_limit = light_limit
        self._cond = threading.Condition()
        self._heavy = 0
        self._light = 0
        self._waiting = []      # 排队中的 job_id（队列位置 = 下标+1）
        self._procs = {}        # job_id → 当前 Popen（用于取消时杀进程树）
        self._cancelled = set()

    @staticmethod
    def is_heavy(job):
        return bool(job.want_transcript)

    def queue_position(self, job_id):
        with self._cond:
            try:
                return self._waiting.index(job_id) + 1
            except ValueError:
                return None

    def acquire(self, job):
        """等待槽位；返回 False 表示排队中被取消。"""
        heavy = self.is_heavy(job)
        with self._cond:
            self._waiting.append(job.id)
            try:
                while True:
                    if job.id in self._cancelled:
                        return False
                    if heavy and self._heavy < self._heavy_limit:
                        self._heavy += 1
                        return True
                    if not heavy and self._light < self._light_limit:
                        self._light += 1
                        return True
                    self._cond.wait(timeout=1.0)
            finally:
                if job.id in self._waiting:
                    self._waiting.remove(job.id)

    def release(self, job):
        with self._cond:
            if self.is_heavy(job):
                self._heavy = max(0, self._heavy - 1)
            else:
                self._light = max(0, self._light - 1)
            self._procs.pop(job.id, None)
            self._cancelled.discard(job.id)
            self._cond.notify_all()

    def register_proc(self, job_id, proc):
        with self._cond:
            if proc is None:
                self._procs.pop(job_id, None)
            else:
                self._procs[job_id] = proc
            cancelled = job_id in self._cancelled
        # 取消可能发生在 Popen 和注册之间；补杀刚启动的本任务进程。
        if proc is not None and cancelled:
            _kill_tree(proc)

    def request_cancel(self, job_id):
        """queued → 标记取消（acquire 返回 False）；running → 杀当前进程树。"""
        with self._cond:
            self._cancelled.add(job_id)
            proc = self._procs.get(job_id)
            self._cond.notify_all()
        if proc is not None:
            _kill_tree(proc)
            return "running"
        return "queued"

    def is_cancelled(self, job_id):
        with self._cond:
            return job_id in self._cancelled


# ---------------------------------------------------------------- 同源媒体键（T03）

_BV_RE = re.compile(r"/(BV[0-9A-Za-z]+)")
# 只剔除明确不影响内容的跟踪参数；内容参数（p、aid、cid、id 等）一律保留
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "vd_source", "from_spmid", "share_source", "share_medium",
    "share_plat", "share_session_id", "share_tag", "bbid", "timestamp",
    "unique_k", "trackid", "from_source",
}


def media_key(input_value, item=None):
    """同源媒体缓存键（T03）。

    - B站链接：BV 号 + 分P（不同分P不同键，不串结果）；
    - 普通 URL：去跟踪参数、保留内容参数的规范化 URL（**不是脱敏 URL**——
      脱敏会丢内容参数导致不同视频误合并）；
    - 本地文件：路径 + mtime + size（内容变化键随之变化）；
    - B站缓存目录：路径 + 所有 .m4s 相对路径、大小与纳秒修改时间指纹。
    """
    s = str(input_value).strip()
    if s.lower().startswith(("http://", "https://")):
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        m = _BV_RE.search(parts.path) if host == "bilibili.com" or host.endswith(".bilibili.com") else None
        if m:
            key = "bili:" + m.group(1)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            item = item or query.get("p") or "1"
        else:
            try:
                parts = urlsplit(s)
                qs = sorted((k, v) for k, v in
                            parse_qsl(parts.query, keep_blank_values=True)
                            if k.lower() not in _TRACKING_PARAMS)
                key = urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                  parts.path, urlencode(qs), ""))
            except Exception:
                key = s
        if item:
            key += f"#item={item}"
        # v2 不读取旧键：旧版批量缓存可能已被别的视频覆盖。
        return "url:v2:" + key
    p = Path(s)
    try:
        if p.is_file():
            st = p.stat()
            return f"file:v2:{p.resolve()}:{st.st_mtime_ns}:{st.st_size}"
        if p.is_dir():
            m4s = sorted(p.rglob("*.m4s"))
            fingerprint = hashlib.sha256()
            for f in m4s:
                try:
                    st = f.stat()
                    fingerprint.update(f"{f.relative_to(p)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8"))
                except OSError:
                    pass
            return f"cache:v2:{p.resolve()}:{len(m4s)}:{fingerprint.hexdigest()}"
    except OSError:
        pass
    return f"raw:{s}"


# ---------------------------------------------------------------- 下载失败分类（T04）

_DL_PATTERNS = [
    ("no_data",
     ("did not get any data", "no data received", "0 bytes", "empty media"),
     "平台未返回数据（CDN 拒绝或访问受限）。可稍后重试，或改用本地文件 / B站缓存入口。"),
    ("connection",
     ("timed out", "timeout", "connection refused", "connection reset",
      "unexpected eof", "ssl", "name or service not known", "unreachable",
      "getaddrinfo"),
     "网络连接问题。检查网络 / 代理后重试。"),
    ("auth",
     ("sign in", "login", "cookies", "region", "not available in your country",
      "copyright", "privilege", "403", "需要登录"),
     "站点需要登录或存在地区 / 版权限制。可换公开视频，或用本地文件 / B站缓存入口。"),
    ("extractor",
     ("unsupported url", "unable to extract", "no suitable extractor",
      "not supported"),
     "当前 yt-dlp 提取器不支持该链接。升级 yt-dlp 或更换链接。"),
]
_RETRYABLE_TYPES = {"connection", "no_data"}


def classify_download_error(text):
    """yt-dlp 错误文本 → {"type", "hint"}（用户可懂的中文分类与建议）；无法归类 None。"""
    low = str(text or "").lower()
    for etype, patterns, hint in _DL_PATTERNS:
        if any(p in low for p in patterns):
            return {"type": etype, "hint": hint}
    return None


def is_retryable(classified):
    """下载阶段是否值得自动重试一次（连接错误 / 无数据）。"""
    return bool(classified) and classified["type"] in _RETRYABLE_TYPES
