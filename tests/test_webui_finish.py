# -*- coding: utf-8 -*-
"""第4批：真实 JPEG 去重、PDF 版式、命中报告、启动诊断回归。"""
import contextlib
import errno
import io
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import launch_webui
import make_frames_pdf as pdf
import webui

FF = common.find_tool("ffmpeg")
FIXTURE = Path(__file__).parent / "fixtures" / "slide_changes.json"


def slide_pixels(addition=False):
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    width, height = spec["width"], spec["height"]
    pixels = bytearray([spec["background"]] * width * height)
    def rect(x, y, w, h):
        for row in range(y, y + h):
            pixels[row * width + x:row * width + x + w] = bytes([20]) * w
    for coords in spec["base_rectangles"]:
        rect(*coords)
    if addition:
        extra = spec["addition"]
        for letter, glyph in enumerate(extra["glyphs"]):
            for row, line in enumerate(glyph):
                for col, bit in enumerate(line):
                    if bit == "1":
                        rect(extra["x"] + (letter * 6 + col) * extra["scale"],
                             extra["y"] + row * extra["scale"], extra["scale"], extra["scale"])
    return b"P5\n%d %d\n255\n" % (width, height) + pixels


@unittest.skipUnless(FF, "需要 ffmpeg")
class RealImageRegressions(unittest.TestCase):
    def run_dedup(self, root, specs):
        frames = root / "frames"
        frames.mkdir()
        entries = []
        for i, (content, pass_id) in enumerate(specs):
            pgm = root / ("input%d.pgm" % i)
            pgm.write_bytes(content)
            name = "frame%d.jpg" % i
            subprocess.run([FF, "-v", "error", "-y", "-i", str(pgm),
                            "-frames:v", "1", str(frames / name)], check=True, timeout=30)
            entries.append({"file": name, "t": float(i), "pass_id": pass_id})
        index = frames / "frames.json"
        index.write_text(json.dumps(entries), encoding="utf-8")
        job = webui.Job("single", ["local"], False, True, dedup={"enabled": True, "threshold": .95})
        job.results = [{"ok": True, "run_dir": str(root), "frames_json": str(index)}]
        webui.dedup_frames(job)
        return json.loads(index.read_text()), job.results[0]

    def test_black_white_not_duplicates_but_identical_white_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            black = b"P5\n64 64\n255\n" + bytes(4096)
            white = b"P5\n64 64\n255\n" + bytes([255]) * 4096
            entries, result = self.run_dedup(Path(tmp), [(black, "base"), (white, "base"), (white, "base")])
            self.assertFalse(entries[1].get("dropped"))
            self.assertTrue(entries[2]["dropped"])
            self.assertEqual(result["dedup"]["kept_count"], 2)

    def test_slide_new_line_kept_then_exact_repeat_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries, result = self.run_dedup(Path(tmp), [(slide_pixels(), "base"),
                (slide_pixels(True), "base"), (slide_pixels(True), "base")])
            paths = [Path(tmp) / "frames" / e["file"] for e in entries]
            gray = [webui._extract_gray16(FF, p) for p in paths[:2]]
            similarity = 1 - webui.hamming(*[webui.ahash_from_gray(g) for g in gray]) / 256
            self.assertGreaterEqual(similarity, .95, "样本必须落在旧算法会隐藏的相似度范围")
            self.assertFalse(entries[1].get("dropped"), "新增 NOTE 不能隐藏")
            self.assertTrue(entries[2]["dropped"], "完全重复仍应去重")
            self.assertEqual(result["dedup"]["kept_count"], 2)

    def test_keyword_frame_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries, result = self.run_dedup(Path(tmp), [(slide_pixels(), "base"), (slide_pixels(), "kw1")])
            self.assertFalse(entries[1].get("dropped"))
            self.assertEqual(result["dedup"]["dropped_count"], 0)

    def test_all_pdf_layout_page_counts_footer_and_escaped_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_dedup(root, [(slide_pixels(), "kw1")] * 7)
            for per_page in (1, 2, 6):
                result = pdf.build_from_run_dir(root, root / ("layout%d.pdf" % per_page),
                                               per_page=per_page, title="PPT (Note) \\ Demo")
                data = Path(result["pdf"]).read_bytes()
                pages = math.ceil(7 / per_page)
                self.assertEqual(result["pages"], pages)
                self.assertEqual(data.count(b"/Type /Page "), pages)
                self.assertIn(b"PPT \\(Note\\) \\\\ Demo", data)
                for page in range(1, pages + 1):
                    self.assertIn(("Page %d / %d" % (page, pages)).encode(), data)


class KeywordReportTests(unittest.TestCase):
    def test_context_multiple_repeated_words_case_and_close_reason(self):
        segments = [{"start": 0, "end": 2, "text": "前文"},
                    {"start": 2, "end": 4, "text": "Hello HELLO world"},
                    {"start": 3, "end": 4, "text": "World"},
                    {"start": 4, "end": 6, "text": "后文"}]
        report = webui.build_keyword_report(segments, ["HELLO", "world", "hello"])
        self.assertEqual(report["total_matched"], 2)
        self.assertEqual(report["sampled_count"], 1)
        self.assertEqual(report["hits"][0]["keywords"], ["hello", "world"])
        self.assertEqual(report["hits"][0]["context_before"], "前文")
        self.assertEqual(report["hits"][1]["reason"], "too_close")

    def test_over_60_report_keeps_all_hits_and_sampling_reasons(self):
        report = webui.build_keyword_report([
            {"start": i * 2, "end": i * 2 + 1, "text": "测试"} for i in range(100)], ["测试"])
        self.assertEqual(report["total_matched"], 100)
        self.assertEqual(report["sampled_count"], 60)
        self.assertEqual(sum(h["reason"] == "sample_limit" for h in report["hits"]), 40)
        self.assertTrue(report["hits"][0]["sampled"] and report["hits"][-1]["sampled"])

    def test_invalid_timestamps_recorded_not_sampled(self):
        segments = [{"start": v, "end": 2, "text": "hi"} for v in (float("nan"), -1, True, "x", 3)]
        report = webui.build_keyword_report(segments, ["hi"])
        self.assertEqual(report["sampled_count"], 0)
        self.assertTrue(all(h["reason"] == "invalid_time" for h in report["hits"]))
        json.dumps(report, allow_nan=False)

    def test_actual_index_reuse_and_new_frame_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); frames = root / "frames"; frames.mkdir()
            report = webui.build_keyword_report([{"start": 0, "end": 2, "text": "hi"},
                                                {"start": 4, "end": 6, "text": "hi"}], ["hi"])
            entries = [{"file": "old.jpg", "t": 1}, {"file": "new.jpg", "t": 5}]
            for e in entries:
                (frames / e["file"]).touch()
            index = frames / "frames.json"; index.write_text(json.dumps(entries))
            result = {"keyword": {"matched": 2, "added": 1}}
            webui._save_keyword_report(result, root, report, frames_json=index, previous_files={"old.jpg"})
            stored = json.loads(Path(result["keyword_report"]).read_text(encoding="utf-8"))
            self.assertEqual([h["reason"] for h in stored["hits"]], ["reused", "added"])
            self.assertTrue(all(h["frame_taken"] for h in stored["hits"]))
            self.assertTrue(all(e["keyword_locked"] for e in json.loads(index.read_text())))
            job = webui.Job("single", ["local"], False, True, dedup={"enabled": True, "threshold": .95})
            job.results = [{"ok": True, "run_dir": str(root), "frames_json": str(index)}]
            webui.dedup_frames(job, extract_fn=lambda p: bytes(256))
            self.assertEqual(job.results[0]["dedup"]["kept_count"], 2,
                             "关键词复用基础帧也应免于隐藏")

    def test_failure_report_keeps_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = webui.build_keyword_report([{"start": 0, "end": 2, "text": "hi"}], ["hi"])
            result = {"keyword": {"matched": 1, "added": 0}}
            webui._save_keyword_report(result, Path(tmp), report, reason="extraction_failed")
            self.assertEqual(report["hits"][0]["reason"], "extraction_failed")
            self.assertFalse(report["hits"][0]["frame_taken"])


class StartupTests(unittest.TestCase):
    def test_version_check_before_webui_import(self):
        with mock.patch.object(sys, "version_info", (3, 9)), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(launch_webui.main([]), 3)
        self.assertIn("版本过低", output.getvalue())

    def test_internal_error_distinct_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "scripts" / "launch_webui.py"
            with mock.patch.object(launch_webui, "__file__", str(fake_path)), \
                    mock.patch.object(webui, "main", side_effect=RuntimeError("TEST_INTERNAL")), \
                    mock.patch.object(webui, "RUNS_ROOT", Path(tmp) / "runs"), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(launch_webui.main([]), 1)
            self.assertIn("启动异常", output.getvalue())
            self.assertIn("TEST_INTERNAL", (Path(tmp) / "runs/webui-startup.log").read_text())

    def test_busy_unrelated_port_does_not_change_jobs(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); sock.listen()
            port = sock.getsockname()[1]
            with mock.patch.object(webui.webui_jobs, "JobStore") as store, \
                    mock.patch.object(webui.urllib.request, "build_opener") as opener, \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                opener.return_value.open.side_effect = OSError("unrelated")
                self.assertEqual(webui.main(["--port", str(port), "--no-browser"]), 4)
                store.assert_not_called()
            self.assertIn("端口占用", output.getvalue())

    def test_existing_same_project_reused_without_mark_interrupted(self):
        server = webui.WebUIServer(("127.0.0.1", 0), webui.WebUIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with mock.patch.object(webui.webui_jobs, "JobStore") as store, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(webui.main(["--port", str(server.server_port), "--no-browser"]), 0)
                store.assert_not_called()
            self.assertIn("已有本项目实例", output.getvalue())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_pdf_rejects_float_or_bool_layout(self):
        for value in (1.0, True, "1", 3):
            with self.assertRaises(ValueError):
                pdf.build_pdf([], per_page=value)

    def test_frame_width_strict_and_transmitted(self):
        for width in (512, 768, 1024):
            params, error = webui.validate_job_request({"mode": "single", "url": "local.mp4", "want_frames": True,
                                                       "frame_width": width})
            self.assertIsNone(error)
            command = webui.build_watch_command("local.mp4", False, True, width=params["frame_width"])
            self.assertEqual(command[command.index("--width") + 1], str(width))
        for width in (1024.0, True, 640, "768"):
            _, error = webui.validate_job_request({"mode": "single", "url": "local.mp4", "frame_width": width})
            self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
