"""README10：状态、资源失效、测试目录与历史隔离回归。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import webui
import webui_jobs
import watch


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.patch = mock.patch.object(webui, "RUNS_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def result(self):
        run = self.root / "video"
        (run / "frames").mkdir(parents=True, exist_ok=True)
        tt = run / "transcript.txt"
        tt.write_text("speech", encoding="utf-8")
        (run / "frames" / "one.jpg").write_bytes(b"image")
        fj = run / "frames" / "frames.json"
        fj.write_text(json.dumps([{"file":"one.jpg", "t":0}]), encoding="utf-8")
        return {"ok":True,"run_dir":str(run),"transcript_txt":str(tt),
                "frames_json":str(fj),"frames_dir":str(fj.parent),"frame_count":None}

    def test_postprocess_failure_is_partial_and_failed_lane_is_not_done(self):
        job = webui.Job("single", ["video.mp4"], True, True)
        r = self.result()
        r["frames_error"] = "间隔抽帧失败"
        job.results = [r]
        job.progress["frames"].update(state="done", percent=100)
        webui.annotate_artifact_status(job)
        status, error = webui.summarize_artifact_status(job)
        self.assertEqual(status, "partial")
        self.assertIn("1/2", error)
        webui._finalize_job_progress(job, status)
        self.assertEqual(job.progress["frames"]["state"], "error")
        self.assertEqual(job.progress["transcript"]["state"], "done")

    def test_frames_only_failure_is_error_and_unselected_transcript_skipped(self):
        job = webui.Job("single", ["video.mp4"], False, True)
        job.results = [{"ok":False,"error":"failure"}]
        webui.annotate_artifact_status(job)
        self.assertEqual(webui.summarize_artifact_status(job)[0], "error")
        self.assertEqual(job.results[0]["artifacts"]["transcript"]["status"], "skipped")

    def test_no_audio_is_legitimate_skip(self):
        job = webui.Job("single", ["video.mp4"], True, True)
        r = self.result()
        r.update(transcript_txt=None, has_audio=False)
        job.results = [r]
        webui.annotate_artifact_status(job)
        self.assertEqual(webui.summarize_artifact_status(job)[0], "done")
        webui._finalize_job_progress(job, "done")
        self.assertEqual(job.progress["transcript"]["state"], "skipped")
        self.assertEqual(r["frame_count"], 1)

    def test_failed_and_skipped_without_output_is_error(self):
        job = webui.Job("single", ["video.mp4"], True, True)
        job.results = [{"ok":True,"has_audio":False,"frames_error":"failed"}]
        webui.annotate_artifact_status(job)
        self.assertEqual(webui.summarize_artifact_status(job)[0], "error")

    def test_keyword_exception_marks_selected_frame_failed(self):
        job = webui.Job("single", ["video.mp4"], True, True, keywords=["test"])
        job.results = [self.result()]
        with mock.patch.object(webui, "keyword_lock_frames", side_effect=RuntimeError("test failure")):
            webui.run_postprocess(job)
        webui.annotate_artifact_status(job)
        self.assertEqual(webui.summarize_artifact_status(job)[0], "partial")
        self.assertIn("test failure", job.results[0]["frames_error"])

    def test_run_job_persists_postprocess_failure_status(self):
        store = webui_jobs.JobStore(self.root / "jobs.db")
        job = webui.Job("single", ["video.mp4"], True, True)
        def post(j):
            j.results[0]["frames_error"] = "postprocess failure"
        with mock.patch.object(webui, "STORE", store), \
             mock.patch.object(webui, "SCHED", webui_jobs.Scheduler()), \
             mock.patch.object(webui, "_run_watch_attempt", return_value=(self.result(), "")), \
             mock.patch.object(webui, "run_postprocess", side_effect=post):
            webui.run_job(job)
        self.assertEqual(job.status, "partial")
        self.assertEqual(store.get(job.id)["status"], "partial")

    def test_missing_files_refresh_availability_without_rewriting_history(self):
        job = webui.Job("single", ["video.mp4"], True, True)
        r = self.result()
        job.results = [r]
        job.status = "done"
        self.assertTrue(job.snapshot()["results"][0]["availability"]["frames"])
        Path(r["transcript_txt"]).unlink()
        (Path(r["frames_dir"]) / "one.jpg").unlink()
        snap = job.snapshot()
        self.assertFalse(snap["results"][0]["availability"]["transcript"])
        self.assertFalse(snap["results"][0]["availability"]["pdf"])
        self.assertEqual(snap["status"], "done")
        self.assertEqual(r["transcript_txt"], snap["results"][0]["transcript_txt"])

    def test_missing_friendly_copy_falls_back_to_original(self):
        r = self.result()
        r["friendly"] = {"transcript_txt":str(self.root / "missing.txt"), "frames_dir":str(self.root / "missing")}
        available = webui.artifact_availability(r)
        self.assertEqual(available["transcript_path"], r["transcript_txt"])
        self.assertEqual(available["frames_path"], r["frames_dir"])

    def test_outside_root_is_not_available(self):
        r = self.result()
        outside = self.root.parent / "outside-private.txt"
        with mock.patch.object(webui, "RUNS_ROOT", self.root / "isolated"):
            self.assertFalse(webui.artifact_availability(r)["transcript"])
            self.assertFalse(webui.artifact_availability(r)["frames"])

    def test_db_snapshot_rechecks_current_files(self):
        r = self.result()
        row = {"job_id":"old", "status":"done", "results":[r]}
        self.assertTrue(webui.db_row_snapshot(row)["results"][0]["availability"]["transcript"])
        Path(r["transcript_txt"]).unlink()
        self.assertFalse(webui.db_row_snapshot(row)["results"][0]["availability"]["transcript"])

    def test_runs_environment_and_episode_use_isolated_root(self):
        with mock.patch.dict(os.environ, {"VIDEO_WATCH_RUNS_DIR":str(self.root)}):
            self.assertEqual(common.runs_dir(), self.root)
            self.assertEqual(watch._episode_run_dir(mock.Mock(out_dir=None), "test", 2).parent, self.root)

    def test_server_isolated_root_does_not_interrupt_daily_history(self):
        daily = self.root / "daily"
        isolated = self.root / "test-runs"
        daily_store = webui_jobs.JobStore(daily / webui_jobs.DB_NAME)
        daily_store.upsert({"job_id":"user-running", "status":"running"})
        server = mock.Mock(server_address=("127.0.0.1",9876))
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(webui, "STORE", None), \
             mock.patch.object(common, "runs_dir", return_value=daily), \
             mock.patch.object(webui, "WebUIServer", return_value=server):
            self.assertEqual(webui.main(["--port","0","--no-browser","--runs-dir",str(isolated)]),0)
            self.assertEqual(webui.RUNS_ROOT, isolated)
            self.assertEqual(os.environ["VIDEO_WATCH_RUNS_DIR"],str(isolated))
            self.assertTrue((isolated / webui_jobs.DB_NAME).is_file())
        self.assertEqual(daily_store.get("user-running")["status"], "running")


if __name__ == "__main__":
    unittest.main()
