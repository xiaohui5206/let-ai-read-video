# -*- coding: utf-8 -*-
"""v2.1.0 回归：转写模型三档贯通（仅文字稿）、PDF 从手动可选变为默认自动产出、
中文友好副本/PDF 带视频标题命名（功能 C）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common  # noqa: E402
import webui  # noqa: E402
import webui_jobs  # noqa: E402

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class ModelValidationTests(unittest.TestCase):
    """model 校验：缺省 small；只接受 tiny/small/medium，bool 与其他类型同样拒绝。"""

    def _payload(self, **overrides):
        payload = {"mode": "single", "url": URL,
                   "want_transcript": True, "want_frames": False}
        payload.update(overrides)
        return payload

    def test_default_model_is_small(self):
        params, error = webui.validate_job_request(self._payload())
        self.assertIsNone(error)
        self.assertEqual(params["model"], "small")

    def test_three_tiers_accepted(self):
        for model in ("tiny", "small", "medium"):
            with self.subTest(model=model):
                params, error = webui.validate_job_request(self._payload(model=model))
                self.assertIsNone(error)
                self.assertEqual(params["model"], model)

    def test_invalid_values_rejected(self):
        for bad in ("large-v3", "base", "", "SMALL"):
            with self.subTest(model=bad):
                params, error = webui.validate_job_request(self._payload(model=bad))
                self.assertIsNone(params)
                self.assertEqual(error, "model 只支持 tiny / small / medium")

    def test_non_string_types_rejected(self):
        for bad in (True, False, 1, 1.5, ["small"], {"m": "small"}):
            with self.subTest(model=bad):
                params, error = webui.validate_job_request(self._payload(model=bad))
                self.assertIsNone(params)
                self.assertEqual(error, "model 只支持 tiny / small / medium")


class BuildCommandModelTests(unittest.TestCase):
    """--model 仅在 want_transcript 时透传 watch.py；与关键帧无关。"""

    def test_model_appended_when_transcript_wanted(self):
        cmd = webui.build_watch_command(URL, want_transcript=True,
                                        want_frames=False, model="medium")
        self.assertEqual(cmd[cmd.index("--model") + 1], "medium")

    def test_model_omitted_for_frames_only(self):
        cmd = webui.build_watch_command(URL, want_transcript=False,
                                        want_frames=True, model="medium")
        self.assertIn("--no-transcribe", cmd)
        self.assertNotIn("--model", cmd)

    def test_model_none_adds_nothing(self):
        cmd = webui.build_watch_command(URL, want_transcript=True,
                                        want_frames=False, model=None)
        self.assertNotIn("--model", cmd)


class AutoPdfValidationTests(unittest.TestCase):
    """auto_pdf 校验：缺省 True；出现时必须为 bool。"""

    def _payload(self, **overrides):
        payload = {"mode": "single", "url": URL,
                   "want_transcript": False, "want_frames": True}
        payload.update(overrides)
        return payload

    def test_default_auto_pdf_is_true(self):
        params, error = webui.validate_job_request(self._payload())
        self.assertIsNone(error)
        self.assertTrue(params["auto_pdf"])

    def test_explicit_bool_accepted(self):
        for value in (True, False):
            with self.subTest(auto_pdf=value):
                params, error = webui.validate_job_request(self._payload(auto_pdf=value))
                self.assertIsNone(error)
                self.assertEqual(params["auto_pdf"], value)

    def test_non_bool_rejected(self):
        for bad in ("true", 1, 0, None, [True]):
            with self.subTest(auto_pdf=bad):
                params, error = webui.validate_job_request(self._payload(auto_pdf=bad))
                self.assertIsNone(params)
                self.assertEqual(error, "auto_pdf 必须是布尔值")


class JobParamsTests(unittest.TestCase):
    """Job 存储 model/auto_pdf 并随 params_dict 持久化（重试用）。"""

    def test_defaults(self):
        job = webui.Job("single", [URL], True, True)
        self.assertEqual(job.model, "small")
        self.assertTrue(job.auto_pdf)

    def test_params_dict_roundtrip(self):
        job = webui.Job("single", [URL], True, True, model="medium", auto_pdf=False)
        params = job.params_dict()
        self.assertEqual(params["model"], "medium")
        self.assertFalse(params["auto_pdf"])
        # 持久化参数经 validate_job_request 重建（前端「重试」会先把 urls[0] 回填 url）
        payload = dict(params)
        payload["url"] = payload["urls"][0]
        rebuilt_params, error = webui.validate_job_request(payload)
        self.assertIsNone(error)
        rebuilt = webui.Job(**rebuilt_params)
        self.assertEqual(rebuilt.model, "medium")
        self.assertFalse(rebuilt.auto_pdf)

    def test_auto_pdf_counts_as_frames_postprocess(self):
        # auto_pdf 计入 frames 通道后处理：「整理结果…」中间态覆盖到 PDF 导完
        self.assertTrue(webui.Job("single", [URL], True, True,
                                  auto_pdf=True).frames_postprocess_pending())
        self.assertFalse(webui.Job("single", [URL], True, True,
                                   auto_pdf=False).frames_postprocess_pending())
        # 纯文字稿通道永不受 auto_pdf 影响
        self.assertFalse(webui.Job("single", [URL], True, False,
                                   auto_pdf=True).frames_postprocess_pending())


class AutoExportPdfTests(unittest.TestCase):
    """run_postprocess 的自动导出：触发条件、逐条容错、失败不影响任务状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sched = webui_jobs.Scheduler()
        patch = mock.patch.object(webui, "SCHED", self.sched)
        patch.start()
        self.addCleanup(patch.stop)

    def _result(self, name="video"):
        run = self.root / name
        (run / "frames").mkdir(parents=True, exist_ok=True)
        (run / "frames" / "one.jpg").write_bytes(b"image")
        tt = run / "transcript.txt"
        tt.write_text("speech", encoding="utf-8")
        fj = run / "frames" / "frames.json"
        fj.write_text(json.dumps([{"file": "one.jpg", "t": 0}]), encoding="utf-8")
        return {"ok": True, "run_dir": str(run), "transcript_txt": str(tt),
                "frames_json": str(fj), "frames_dir": str(fj.parent), "frame_count": 1}

    def test_auto_pdf_exports_each_frames_result(self):
        job = webui.Job("batch", [URL, URL + "?p=2"], False, True, auto_pdf=True)
        first = self._result("a")
        job.results = [first,
                       {"ok": False, "error": "boom"},                 # 失败条目不导
                       {"ok": True, "run_dir": str(self.root / "b")}]  # 无帧产物不导
        with mock.patch.object(webui, "run_export_pdf",
                               return_value={"ok": True, "pdf": "x/关键帧.pdf"}) as export:
            webui.run_postprocess(job)
        export.assert_called_once_with(first["run_dir"], per_page=6)
        self.assertTrue(any("已自动导出 关键帧.pdf" in line for line in job._logs))

    def test_export_failure_only_logs_and_never_marks_frames_error(self):
        job = webui.Job("single", [URL], True, True, auto_pdf=True)
        job.results = [self._result()]
        with mock.patch.object(webui, "run_export_pdf",
                               return_value={"ok": False, "error": "导出超时（>120s）"}):
            webui.run_postprocess(job)
        self.assertNotIn("frames_error", job.results[0])
        self.assertTrue(any("自动导出 PDF 失败（不影响任务）" in line for line in job._logs))
        # 任务状态照常按产物汇总为完成
        webui.annotate_artifact_status(job)
        status, _ = webui.summarize_artifact_status(job)
        self.assertEqual(status, "done")

    def test_export_exception_is_swallowed_per_result(self):
        job = webui.Job("batch", [URL, URL + "?p=2"], False, True, auto_pdf=True)
        first, second = self._result("a"), self._result("b")
        job.results = [first, second]
        calls = []
        def flaky(run_dir, per_page=6):
            calls.append(run_dir)
            if run_dir == first["run_dir"]:
                raise RuntimeError("导出进程崩溃")
            return {"ok": True, "pdf": run_dir + "/关键帧.pdf"}
        with mock.patch.object(webui, "run_export_pdf", side_effect=flaky):
            webui.run_postprocess(job)   # 不抛出
        self.assertEqual(calls, [first["run_dir"], second["run_dir"]])
        for r in job.results:
            self.assertNotIn("frames_error", r)

    def test_auto_pdf_disabled_skips_export(self):
        job = webui.Job("single", [URL], False, True, auto_pdf=False)
        job.results = [self._result()]
        with mock.patch.object(webui, "run_export_pdf") as export:
            webui.run_postprocess(job)
        export.assert_not_called()

    def test_cancelled_before_postprocess_never_exports(self):
        job = webui.Job("single", [URL], False, True, auto_pdf=True)
        job.results = [self._result()]
        self.sched.request_cancel(job.id)
        with mock.patch.object(webui, "run_export_pdf") as export:
            webui.run_postprocess(job)
        export.assert_not_called()


class RunJobV210Tests(unittest.TestCase):
    """任务执行链路：model 透传子进程命令、auto_pdf 后处理接入 run_job。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = webui_jobs.JobStore(self.root / "jobs.db")
        self.sched = webui_jobs.Scheduler()
        self.patches = [mock.patch.object(webui, "STORE", self.store),
                        mock.patch.object(webui, "SCHED", self.sched)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_command_carries_selected_model(self):
        job = webui.Job("single", [URL], True, False, model="medium")
        with mock.patch.object(webui, "_run_watch_attempt",
                               return_value=({"ok": False, "error": "stop"}, "")) as attempt:
            webui.run_job(job)
        cmd = attempt.call_args.args[1]
        self.assertEqual(cmd[cmd.index("--model") + 1], "medium")

    def test_frames_only_command_has_no_model(self):
        job = webui.Job("single", [URL], False, True, auto_pdf=False)
        with mock.patch.object(webui, "_run_watch_attempt",
                               return_value=({"ok": False, "error": "stop"}, "")) as attempt:
            webui.run_job(job)
        self.assertNotIn("--model", attempt.call_args.args[1])

    def _frames_result(self):
        run = self.root / "video"
        (run / "frames").mkdir(parents=True, exist_ok=True)
        (run / "frames" / "one.jpg").write_bytes(b"image")
        fj = run / "frames" / "frames.json"
        fj.write_text(json.dumps([{"file": "one.jpg", "t": 0}]), encoding="utf-8")
        return {"ok": True, "run_dir": str(run), "frames_json": str(fj),
                "frames_dir": str(fj.parent), "frame_count": 1, "title": "t"}

    def test_run_job_auto_exports_pdf_after_completion(self):
        result = self._frames_result()
        job = webui.Job("single", [URL], False, True, auto_pdf=True)
        with mock.patch.object(webui, "_run_watch_attempt", return_value=(result, "")), \
             mock.patch.object(webui, "run_export_pdf",
                               return_value={"ok": True,
                                             "pdf": result["run_dir"] + "/关键帧.pdf"}) as export:
            webui.run_job(job)
        export.assert_called_once_with(result["run_dir"], per_page=6)
        self.assertEqual(job.status, "done")
        self.assertEqual(self.store.get(job.id)["status"], "done")

    def test_run_job_export_failure_keeps_done_status(self):
        result = self._frames_result()
        job = webui.Job("single", [URL], False, True, auto_pdf=True)
        with mock.patch.object(webui, "_run_watch_attempt", return_value=(result, "")), \
             mock.patch.object(webui, "run_export_pdf",
                               return_value={"ok": False, "error": "导出超时（>120s）"}):
            webui.run_job(job)
        self.assertEqual(job.status, "done")
        self.assertNotIn("frames_error", job.results[0])


class FriendlyNamingTests(unittest.TestCase):
    """功能 C：中文友好副本/PDF 带视频标题命名；旧通用名回退识别（老任务兼容）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, name, title=None):
        run = self.root / name
        (run / "frames").mkdir(parents=True)
        if title is not None:
            (run / "manifest.json").write_text(
                json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8")
        (run / "transcript.txt").write_text("[00:01] 你好", encoding="utf-8")
        (run / "transcript.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n你好",
                                            encoding="utf-8")
        (run / "frames" / "one.jpg").write_bytes(b"\xff\xd8image")
        (run / "frames" / "frames.json").write_text(
            json.dumps([{"file": "one.jpg", "t": 0}]), encoding="utf-8")
        return {"ok": True, "run_dir": str(run),
                "transcript_txt": str(run / "transcript.txt"),
                "frames_json": str(run / "frames" / "frames.json"),
                "frames_dir": str(run / "frames"), "frame_count": 1}

    def test_multi_episode_each_uses_own_title(self):
        # 多P/批量：每个 result 按各自 run_dir 的 manifest 标题命名
        job = webui.Job("batch", [URL, URL + "?p=2"], True, True, auto_pdf=False)
        job.results = [self._run("p01", "【教程】AI 入门: p01/上?"),
                       self._run("p02", "第二集 different title")]
        webui.friendly_outputs(job)
        run1 = Path(job.results[0]["run_dir"])
        run2 = Path(job.results[1]["run_dir"])
        self.assertTrue((run1 / "教程-ai-入门-p01-上_文字稿.txt").is_file())
        self.assertTrue((run1 / "教程-ai-入门-p01-上_文字稿.srt").is_file())
        self.assertTrue((run1 / "教程-ai-入门-p01-上_关键帧" / "one.jpg").is_file())
        self.assertTrue((run2 / "第二集-different-title_文字稿.txt").is_file())
        # 原始产物保留不动（AI 工作流兼容）
        self.assertTrue((run1 / "transcript.txt").is_file())
        self.assertTrue((run2 / "frames" / "frames.json").is_file())
        self.assertTrue(job.results[0]["friendly"]["transcript_txt"]
                        .endswith("教程-ai-入门-p01-上_文字稿.txt"))

    def test_title_sanitized_and_truncated_like_run_dir(self):
        # 清洗口径与 run 目录命名一致（复用 common.slugify），最长 50 字符
        r = self._run("video", 'a/b\\c:d*e?f"g<h>i|j')
        self.assertEqual(webui._friendly_title(r["run_dir"]), "a-b-c-d-e-f-g-h-i-j")
        r2 = self._run("long", "x" * 100)
        prefix = webui._friendly_title(r2["run_dir"])
        self.assertEqual(len(prefix), 50)
        self.assertEqual(prefix, common.slugify("x" * 100, max_len=50))

    def test_no_title_falls_back_to_generic_names(self):
        # 老任务兼容：manifest 缺失且无标题时沿用旧通用名
        job = webui.Job("single", [URL], True, True, auto_pdf=False)
        job.results = [self._run("old")]
        webui.friendly_outputs(job)
        run = Path(job.results[0]["run_dir"])
        self.assertTrue((run / "文字稿.txt").is_file())
        self.assertTrue((run / "关键帧" / "one.jpg").is_file())

    def _fake_pdf_proc(self, run):
        payload = {"ok": True, "pdf": str(Path(run) / "关键帧.pdf"),
                   "pages": 1, "frames_included": 1}
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=webui.RESULT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n",
            stderr="")

    def test_export_pdf_renamed_with_title(self):
        run = Path(self._run("video", "我的视频")["run_dir"])
        (run / "关键帧.pdf").write_bytes(b"%PDF fake")
        with mock.patch("subprocess.run", return_value=self._fake_pdf_proc(run)):
            result = webui.run_export_pdf(run, per_page=6)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pdf"].endswith("我的视频_关键帧.pdf"))
        self.assertTrue((run / "我的视频_关键帧.pdf").is_file())
        self.assertFalse((run / "关键帧.pdf").exists())

    def test_export_pdf_without_title_keeps_generic_name(self):
        # 取不到标题保留通用名（旧行为不变）
        run = Path(self._run("video")["run_dir"])
        (run / "关键帧.pdf").write_bytes(b"%PDF fake")
        with mock.patch("subprocess.run", return_value=self._fake_pdf_proc(run)):
            result = webui.run_export_pdf(run, per_page=2)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["pdf"]).name, "关键帧.pdf")
        self.assertTrue((run / "关键帧.pdf").is_file())

    def test_restore_copies_into_titled_frames_dir(self):
        run = Path(self._run("video", "我的视频")["run_dir"])
        (run / "frames" / "frames.json").write_text(
            json.dumps([{"file": "one.jpg", "t": 0, "dropped": True}]), encoding="utf-8")
        copy_dir = run / "我的视频_关键帧"
        copy_dir.mkdir()
        result = webui.dedup_restore(run, "one.jpg")
        self.assertTrue(result["ok"])
        self.assertTrue((copy_dir / "one.jpg").is_file())

    def test_restore_falls_back_to_legacy_frames_dir(self):
        # 旧任务只有通用名 关键帧/ 目录：恢复仍能补拷（旧名回退识别）
        run = Path(self._run("old")["run_dir"])
        (run / "frames" / "frames.json").write_text(
            json.dumps([{"file": "one.jpg", "t": 0, "dropped": True}]), encoding="utf-8")
        (run / "关键帧").mkdir()
        result = webui.dedup_restore(run, "one.jpg")
        self.assertTrue(result["ok"])
        self.assertTrue((run / "关键帧" / "one.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
