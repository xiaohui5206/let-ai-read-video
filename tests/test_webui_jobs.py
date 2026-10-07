# -*- coding: utf-8 -*-
"""webui_jobs.py 单元测试：sqlite 持久化 round-trip、调度器槽位/取消、
媒体键规范化、下载失败分类。无网络、无真实媒体。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import webui_jobs  # noqa: E402


def _rec(job_id, status="done", **kw):
    base = {"job_id": job_id, "mode": "single",
            "params": json.dumps({"mode": "single"}),
            "status": status,
            "progress": json.dumps({}), "results": json.dumps([]),
            "logs": json.dumps(["l1"]), "error": None,
            "media_key": "k:" + job_id,
            "created_at": webui_jobs.now_iso(), "finished_at": None}
    base.update(kw)
    return base


class JobStoreTests(unittest.TestCase):
    def test_upsert_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            store.upsert(_rec("j1", status="running"))
            store.upsert(_rec("j1", status="done", error=None))
            row = store.get("j1")
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["params"], {"mode": "single"})
            self.assertIsNone(store.get("nope"))

    def test_list_desc_and_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            for i in range(5):
                store.upsert(_rec(f"j{i}", created_at=f"2026-01-01T00:00:0{i}"))
            page1 = store.list(limit=2, offset=0)
            page2 = store.list(limit=2, offset=2)
            self.assertEqual([r["job_id"] for r in page1], ["j4", "j3"])
            self.assertEqual([r["job_id"] for r in page2], ["j2", "j1"])

    def test_mark_interrupted_only_unfinished(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            store.upsert(_rec("run1", status="running"))
            store.upsert(_rec("q1", status="queued"))
            store.upsert(_rec("ok1", status="done"))
            n = store.mark_interrupted()
            self.assertEqual(n, 2)
            self.assertEqual(store.get("run1")["status"], "interrupted")
            self.assertEqual(store.get("q1")["status"], "interrupted")
            self.assertEqual(store.get("ok1")["status"], "done")

    def test_media_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            self.assertIsNone(store.media_get("k"))
            store.media_set("k", "runs/a", "runs/a/v.mp4")
            store.media_set("k", "runs/b", "runs/b/v.mp4")   # 覆盖更新
            row = store.media_get("k")
            self.assertEqual(row["run_dir"], "runs/b")


class _FakeJob:
    def __init__(self, job_id, heavy):
        self.id = job_id
        self.want_transcript = heavy


class SchedulerTests(unittest.TestCase):
    def test_heavy_slot_serializes_and_queue_position_visible(self):
        sched = webui_jobs.Scheduler(heavy_limit=1, light_limit=2)
        a, b = _FakeJob("a", True), _FakeJob("b", True)
        self.assertTrue(sched.acquire(a))
        acquired = []
        t = threading.Thread(target=lambda: acquired.append(sched.acquire(b)), daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertEqual(acquired, [])               # b 在排队
        self.assertEqual(sched.queue_position("b"), 1)
        sched.release(a)
        t.join(timeout=3)
        self.assertEqual(acquired, [True])
        self.assertIsNone(sched.queue_position("b"))
        sched.release(b)

    def test_light_jobs_run_two_concurrently(self):
        sched = webui_jobs.Scheduler(heavy_limit=1, light_limit=2)
        a, b = _FakeJob("a", False), _FakeJob("b", False)
        self.assertTrue(sched.acquire(a))
        self.assertTrue(sched.acquire(b))            # 轻型第二个直接拿槽
        sched.release(a)
        sched.release(b)

    def test_cancel_queued_makes_acquire_false(self):
        sched = webui_jobs.Scheduler(heavy_limit=1)
        a, b = _FakeJob("a", True), _FakeJob("b", True)
        self.assertTrue(sched.acquire(a))
        result = []
        t = threading.Thread(target=lambda: result.append(sched.acquire(b)), daemon=True)
        t.start()
        time.sleep(0.3)
        sched.request_cancel("b")
        t.join(timeout=3)
        self.assertEqual(result, [False])            # 排队中被取消
        sched.release(a)

    def test_cancel_running_kills_process_tree(self):
        sched = webui_jobs.Scheduler()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.pid = 43210
        sched.register_proc("j1", proc)
        with mock.patch.object(webui_jobs, "_kill_tree") as kill:
            phase = sched.request_cancel("j1")
        self.assertEqual(phase, "running")
        kill.assert_called_once_with(proc)
        self.assertTrue(sched.is_cancelled("j1"))


class MediaKeyTests(unittest.TestCase):
    def test_bilibili_uses_bv_and_item(self):
        k1 = webui_jobs.media_key("https://www.bilibili.com/video/BV1xx411c7mD?p=3&spm=x", "3")
        k2 = webui_jobs.media_key("https://www.bilibili.com/video/BV1xx411c7mD?p=5", "5")
        self.assertIn("BV1xx411c7mD", k1)
        self.assertNotEqual(k1, k2)                  # 不同分P不串
        self.assertNotIn("spm", k1)

    def test_url_strips_tracking_keeps_content_params(self):
        k = webui_jobs.media_key(
            "https://Example.com/v?id=42&utm_source=share&p=2&bbid=x")
        self.assertIn("id=42", k)
        self.assertIn("p=2", k)                      # 内容参数保留
        self.assertNotIn("utm_source", k)
        self.assertNotIn("bbid", k)
        # 同内容不同跟踪参数 → 同键
        k2 = webui_jobs.media_key("https://example.com/v?p=2&id=42&utm_campaign=y")
        self.assertEqual(k, k2)

    def test_file_key_depends_on_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.mp4"
            f.write_bytes(b"123")
            k1 = webui_jobs.media_key(str(f))
            f.write_bytes(b"12345678")               # size 变化 → 键变化
            k2 = webui_jobs.media_key(str(f))
            self.assertNotEqual(k1, k2)

    def test_cache_dir_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "cid1"
            d.mkdir()
            (d / "a.m4s").write_bytes(b"x" * 10)
            k1 = webui_jobs.media_key(str(d))
            (d / "b.m4s").write_bytes(b"y" * 20)
            k2 = webui_jobs.media_key(str(d))
            self.assertNotEqual(k1, k2)


class ClassifyErrorTests(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(webui_jobs.classify_download_error(
            "ERROR: Did not get any data blocks")["type"], "no_data")
        self.assertEqual(webui_jobs.classify_download_error(
            "SSL: UNEXPECTED_EOF_WHILE_READING")["type"], "connection")
        self.assertEqual(webui_jobs.classify_download_error(
            "Sign in to confirm your age")["type"], "auth")
        self.assertEqual(webui_jobs.classify_download_error(
            "Unsupported URL: https://x")["type"], "extractor")
        self.assertIsNone(webui_jobs.classify_download_error("磁盘已满"))
        self.assertTrue(webui_jobs.is_retryable({"type": "no_data"}))
        self.assertFalse(webui_jobs.is_retryable({"type": "auth"}))
        # 分类含中文建议动作
        cls = webui_jobs.classify_download_error("Did not get any data")
        self.assertIn("本地文件", cls["hint"])


if __name__ == "__main__":
    unittest.main()
