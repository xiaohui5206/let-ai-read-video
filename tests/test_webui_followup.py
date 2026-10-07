"""二次检查回归：缓存隔离、恢复后的历史、文字稿参数、真实子进程取消。"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import webui
import webui_jobs

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = webui_jobs.JobStore(self.root / "jobs.db")
        self.sched = webui_jobs.Scheduler()
        self.patches = [mock.patch.object(webui, "STORE", self.store),
                        mock.patch.object(webui, "SCHED", self.sched)]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.temp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def test_bili_query_part_and_host_are_distinct(self):
        self.assertNotEqual(webui_jobs.media_key(URL + "?p=2"), webui_jobs.media_key(URL + "?p=3"))
        self.assertEqual(webui_jobs.media_key(URL + "?p=2"), webui_jobs.media_key(URL, "2"))
        self.assertEqual(webui_jobs.media_key(URL), webui_jobs.media_key(URL + "?p=1"))
        self.assertNotEqual(webui_jobs.media_key(URL), webui_jobs.media_key(URL.replace("www.bilibili.com", "example.org")))

    def _media(self, name):
        run = self.root / name
        run.mkdir()
        video = run / "video.mp4"
        video.write_bytes(name.encode())
        (run / "manifest.json").write_text(json.dumps({"video_path": str(video)}), encoding="utf-8")
        return run, video

    def test_batch_reads_and_writes_cache_per_input(self):
        urls = [URL + "?p=2", URL + "?p=3"]
        videos = []
        for i, url in enumerate(urls):
            run, video = self._media(str(i))
            videos.append(str(video))
            self.store.media_set(webui_jobs.media_key(url), str(run), str(video))
        commands = []

        def watch(job, cmd, env):
            commands.append(cmd)
            run, video = self._media("new" + str(len(commands)))
            fj = run / "frames.json"
            fj.write_text('[{"file":"frame.jpg"}]', encoding="utf-8")
            return {"ok": True, "run_dir": str(run), "frames_json":str(fj), "frame_count":1}, ""

        job = webui.Job("batch", urls, False, True)
        with mock.patch.object(webui, "_run_watch_attempt", side_effect=watch):
            webui.run_job(job)
        self.assertEqual(job.status, "done")
        self.assertIn(videos[0], commands[0])
        self.assertIn(videos[1], commands[1])
        for i, url in enumerate(urls, 1):
            self.assertEqual(Path(self.store.media_get(webui_jobs.media_key(url))["run_dir"]).name, "new" + str(i))
        self.assertIsNone(job.media_key)

    def test_all_selection_never_reuses_one_episode(self):
        run, video = self._media("old")
        self.store.media_set(webui_jobs.media_key(URL, "all"), str(run), str(video))
        job = webui.Job("multi", [URL], False, True, item="all")
        with mock.patch.object(webui, "_run_watch_attempt", return_value=({"ok": False}, "")) as watch:
            webui.run_job(job)
        self.assertIn(URL, watch.call_args.args[1])
        self.assertNotIn(str(video), watch.call_args.args[1])

    def test_cached_single_episode_drops_playlist_selector(self):
        run, video = self._media("old")
        self.store.media_set(webui_jobs.media_key(URL, "2"), str(run), str(video))
        job = webui.Job("batch", [{"url": URL, "item": "2"}], False, True)
        with mock.patch.object(webui, "_run_watch_attempt", return_value=({"ok": False}, "")) as watch:
            webui.run_job(job)
        self.assertIn(str(video), watch.call_args.args[1])
        self.assertNotIn("--item", watch.call_args.args[1])

    def test_transcript_only_ignores_frame_settings(self):
        params, error = webui.validate_job_request({"mode": "single", "url": URL,
            "want_transcript": True, "want_frames": False,
            "frame_rule": {"type": "count", "value": ""}, "keyword": "test",
            "frame_width": "broken", "dedup": {"enabled": True, "threshold": ""}})
        self.assertIsNone(error)
        job = webui.Job(**params)
        self.assertEqual(job.keywords, [])
        self.assertFalse(job.frames_postprocess_pending())
        with mock.patch.object(webui, "keyword_lock_frames") as kw, mock.patch.object(webui, "dedup_frames") as dedup:
            webui.run_postprocess(job)
        kw.assert_not_called()
        dedup.assert_not_called()

    def test_restore_counts_survive_reopen_and_concurrent_restore(self):
        frames = self.root / "frames"
        frames.mkdir()
        (self.root / "关键帧").mkdir()
        entries = [{"file": str(i) + ".jpg", "dropped": True} for i in range(2)]
        for e in entries:
            (frames / e["file"]).write_bytes(b"image")
        (frames / "frames.json").write_text(json.dumps(entries), encoding="utf-8")
        job = webui.Job("single", [URL], False, True)
        job.results = [{"ok": True, "run_dir": str(self.root), "dedup": {
            "total_count": 2, "kept_count": 0, "dropped_count": 2}, "friendly": {"frame_count": 0}}]
        webui.persist_job(job, force=True)
        results = []
        threads = [threading.Thread(target=lambda f=e["file"]: results.append(webui.dedup_restore(self.root, f))) for e in entries]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        reopened = webui_jobs.JobStore(self.root / "jobs.db")
        for snapshot in [job.snapshot(), webui.db_row_snapshot(reopened.get(job.id))]:
            r = snapshot["results"][0]
            self.assertEqual(r["dedup"]["kept_count"], 2)
            self.assertEqual(r["dedup"]["dropped_count"], 0)
            self.assertEqual(r["friendly"]["frame_count"], 2)
        self.assertEqual(len(list((self.root / "关键帧").glob("*.jpg"))), 2)

    def test_cancel_between_spawn_and_registration_kills_process(self):
        self.sched.request_cancel("race")
        proc = mock.Mock()
        with mock.patch.object(webui_jobs, "_kill_tree") as kill:
            self.sched.register_proc("race", proc)
        kill.assert_called_once_with(proc)

    def test_cancel_during_postprocess_is_not_done(self):
        job = webui.Job("single", [URL], False, True)
        with mock.patch.object(webui, "_run_watch_attempt", return_value=({"ok": True}, "")), \
             mock.patch.object(webui, "run_postprocess", side_effect=lambda j: self.sched.request_cancel(j.id)):
            webui.run_job(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(self.store.get(job.id)["status"], "cancelled")

    def test_cancel_waiting_for_media_lock_is_prompt(self):
        job = webui.Job("single", [URL], False, True)
        lock = webui.media_lock(job.media_key)
        lock.acquire()
        thread = threading.Thread(target=webui.run_job, args=(job,))
        thread.start()
        try:
            self.sched.request_cancel(job.id)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(job.status, "cancelled")
        finally:
            lock.release()
            thread.join(3)

    def test_real_postprocess_child_is_cancelled(self):
        job = webui.Job("single", [URL], False, True)
        ready = self.root / "ready"
        cmd = [sys.executable, "-c", "import pathlib,time; pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(60)"]
        results = []
        thread = threading.Thread(target=lambda: results.append(webui._run_cancellable_command(job, cmd, timeout=10)))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "child must start before cancellation")
            self.sched.request_cancel(job.id)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertNotEqual(results[0].returncode, 0)
        finally:
            self.sched.request_cancel(job.id)
            thread.join(12)


if __name__ == "__main__":
    unittest.main()
