# -*- coding: utf-8 -*-
"""webui.py 单元测试：命令拼装、RESULT_JSON 解析、进度解析、路径安全、HTTP 端到端（mock 子进程）。

无网络、无真实媒体；服务器起在 127.0.0.1:0（临时端口），子进程用 mock 的
subprocess.Popen 顶替，stdout 末行喂 RESULT_JSON 走通任务生命周期。
"""
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import webui  # noqa: E402
import webui_jobs  # noqa: E402

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


# ---------------------------------------------------------------- 命令拼装


class BuildCommandTests(unittest.TestCase):
    """三种产物组合 × 三种模式的参数拼装。"""

    def _check_base(self, cmd, url):
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("watch.py"))
        self.assertIn(url, cmd)
        self.assertIn("--no-review", cmd)

    def test_transcript_only(self):
        for mode in ("single", "multi", "batch"):
            with self.subTest(mode=mode):
                item = "3-7" if mode == "multi" else None
                cmd = webui.build_watch_command(URL, want_transcript=True,
                                                want_frames=False, item=item)
                self._check_base(cmd, URL)
                self.assertIn("--no-frames", cmd)
                self.assertNotIn("--no-transcribe", cmd)

    def test_frames_only(self):
        for mode in ("single", "multi", "batch"):
            with self.subTest(mode=mode):
                item = "3-7" if mode == "multi" else None
                cmd = webui.build_watch_command(URL, want_transcript=False,
                                                want_frames=True, item=item)
                self._check_base(cmd, URL)
                self.assertNotIn("--no-frames", cmd)
                self.assertIn("--no-transcribe", cmd)

    def test_both_products(self):
        for mode in ("single", "multi", "batch"):
            with self.subTest(mode=mode):
                item = "3-7" if mode == "multi" else None
                cmd = webui.build_watch_command(URL, want_transcript=True,
                                                want_frames=True, item=item)
                self._check_base(cmd, URL)
                self.assertNotIn("--no-frames", cmd)
                self.assertNotIn("--no-transcribe", cmd)

    def test_multi_appends_item(self):
        for item in ("3", "3-7", "all"):
            with self.subTest(item=item):
                cmd = webui.build_watch_command(URL, item=item)
                self.assertEqual(cmd[cmd.index("--item") + 1], item)

    def test_single_and_batch_never_carry_item(self):
        for cmd in (webui.build_watch_command(URL),
                    webui.build_watch_command("https://a.example/1"),
                    webui.build_watch_command("https://a.example/2")):
            self.assertNotIn("--item", cmd)

    # ---- 关键帧截取原则（frame_rule，C03 语义）----

    def test_frame_rule_count(self):
        # 自定义帧数 = 目标 N 帧 → --budget N（而非 --max-frames 上限，C03）
        cmd = webui.build_watch_command(URL, want_transcript=True, want_frames=True,
                                        frame_rule={"type": "count", "value": 30})
        self.assertEqual(cmd[cmd.index("--budget") + 1], "30")
        self.assertNotIn("--max-frames", cmd)
        self.assertNotIn("--fps", cmd)

    def test_frame_rule_interval_not_in_watch_cmd(self):
        # 自定义间隔不走 watch.py 拼参：由 webui 事后 frames.py --times-json 显式执行
        cmd = webui.build_watch_command(URL, want_frames=True,
                                        frame_rule={"type": "interval", "value": 5})
        self.assertNotIn("--fps", cmd)
        self.assertNotIn("--max-frames", cmd)
        self.assertNotIn("--budget", cmd)

    def test_frame_rule_default_adds_nothing(self):
        for rule in (None, {"type": "default"}):
            with self.subTest(rule=rule):
                cmd = webui.build_watch_command(URL, frame_rule=rule)
                self.assertNotIn("--max-frames", cmd)
                self.assertNotIn("--fps", cmd)

    def test_frame_rule_ignored_when_frames_not_wanted(self):
        cmd = webui.build_watch_command(URL, want_transcript=True, want_frames=False,
                                        frame_rule={"type": "count", "value": 30})
        self.assertIn("--no-frames", cmd)
        self.assertNotIn("--max-frames", cmd)
        self.assertNotIn("--fps", cmd)


# ---------------------------------------------------------------- RESULT_JSON 解析


class ResultParsingTests(unittest.TestCase):
    def test_parse_single_format(self):
        out = ("[watch] 1/4 探测输入\n[watch] ✅ 完成\n"
               + webui.RESULT_PREFIX
               + json.dumps({"ok": True, "run_dir": "runs/x", "title": "测试"},
                            ensure_ascii=False))
        result = webui.parse_result_json(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "测试")

    def test_parse_finds_last_line_even_with_trailing_noise(self):
        # stderr 并入日志流后，RESULT_JSON 之后可能混入错误行，倒序查找仍命中
        out = (webui.RESULT_PREFIX + '{"ok": false, "error": "x"}\n'
               "[watch] ERROR: 详情写在后面\n")
        self.assertEqual(webui.parse_result_json(out)["error"], "x")

    def test_parse_missing_or_invalid_returns_none(self):
        self.assertIsNone(webui.parse_result_json("[watch] 只有日志\n"))
        self.assertIsNone(webui.parse_result_json(
            webui.RESULT_PREFIX + "{不是合法JSON"))

    def test_extract_single_result(self):
        results = webui.extract_results({
            "ok": True, "run_dir": "runs/t", "title": "标题",
            "transcript_txt": "runs/t/transcript.txt",
            "frames_dir": "runs/t/frames",
            "frames_json": "runs/t/frames/frames.json", "frame_count": 42,
        }, URL)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "标题")
        self.assertEqual(r["frame_count"], 42)
        self.assertEqual(r["url"], URL)

    def test_extract_multi_aggregate_expands_episodes(self):
        results = webui.extract_results({
            "ok": True, "succeeded": 1, "failed": 1, "total": 2,
            "episodes": [
                {"item": 3, "ok": True, "run_dir": "runs/t_p03",
                 "transcript_txt": "runs/t_p03/transcript.txt",
                 "frames_json": "runs/t_p03/frames/frames.json"},
                {"item": 4, "ok": False, "run_dir": "runs/t_p04",
                 "transcript_txt": None, "frames_json": None,
                 "error": "下载失败"},
            ],
        }, URL)
        self.assertEqual(len(results), 2)
        ok_ep, bad_ep = results
        self.assertTrue(ok_ep["ok"])
        self.assertEqual(ok_ep["title"], "第 3 集")
        self.assertEqual(ok_ep["frames_dir"], str(Path("runs/t_p03/frames")))
        self.assertFalse(bad_ep["ok"])
        self.assertEqual(bad_ep["error"], "下载失败")
        self.assertIsNone(bad_ep["frames_dir"])


# ---------------------------------------------------------------- 进度解析


def _both_job():
    return webui.Job("single", [URL], True, True)


class ProgressParsingTests(unittest.TestCase):
    """update_progress：日志锚点驱动双通道进度（锚行取自脚本真实日志）。"""

    def _percents(self, job, lane, lines):
        seq = []
        for line in lines:
            webui.update_progress(job, line)
            seq.append(job.progress[lane]["percent"])
        return seq

    def test_transcript_full_sequence_reaches_100(self):
        job = _both_job()
        lines = [
            "[watch] ▶ 1/4 探测输入 (probe)",
            "[watch] ▶ 2/4 下载视频 (download)",
            "[watch] 3/4 转写：无可用字幕，使用 faster-whisper (model=small)",
            "  [transcribe] [video-watch] 加载 faster-whisper 模型 small（device=cuda）…",
            "  [transcribe] [video-watch] 转写完成：128 个 segment，语言 zh",
            "  [transcribe] [video-watch] 已写出: transcript.srt / transcript.txt / transcript.json（128 segments）",
            "[watch] ✅ 完成",
        ]
        seq = self._percents(job, "transcript", lines)
        self.assertEqual(seq, [5, 15, 45, 55, 85, 95, 100])   # 单调递增至 100
        self.assertEqual(job.progress["transcript"]["state"], "done")
        self.assertEqual(job.progress["transcript"]["stage"], "完成")

    def test_transcript_caption_variant(self):
        job = _both_job()
        webui.update_progress(job, "[watch] 3/4 转写：使用平台字幕 lang=zh kind=manual")
        webui.update_progress(job, "  [transcribe] [video-watch] 解析字幕文件: captions.zh.vtt")
        self.assertEqual(job.progress["transcript"]["percent"], 55)
        webui.update_progress(job, "  [transcribe] [video-watch] 字幕解析完成：96 个 segment（已去重合并）")
        self.assertEqual(job.progress["transcript"]["percent"], 85)

    def test_frames_progress_formula(self):
        job = _both_job()
        webui.update_progress(job, "[watch] ▶ 4/4 抽帧 (frames)")
        self.assertEqual(job.progress["frames"]["percent"], 5)
        webui.update_progress(job, "  [frames] [video-watch] 视频时长 300.0s；抽帧窗口 [00:00 – 05:00]（300.0s）")
        self.assertEqual(job.progress["frames"]["percent"], 5)
        webui.update_progress(job, "  [frames] [video-watch] 场景检测中（阈值 0.3）…")
        self.assertEqual(job.progress["frames"]["percent"], 8)
        webui.update_progress(job, "  [frames] [video-watch] 选点完成：场景点 30 + 均匀点 50 = 80")
        self.assertEqual(job.progress["frames"]["percent"], 12)
        self.assertIn("80", job.progress["frames"]["stage"])
        webui.update_progress(job, "  [frames] [video-watch] 抽帧进度 40/80")
        self.assertAlmostEqual(job.progress["frames"]["percent"], 12 + 83 * 0.5, delta=0.5)
        self.assertIn("40/80", job.progress["frames"]["stage"])
        webui.update_progress(job, "  [frames] [video-watch] 完成：新增 80 帧，索引共 80 帧 → frames")
        self.assertEqual(job.progress["frames"]["percent"], 98)
        webui.update_progress(job, "[watch] ✅ 完成")
        self.assertEqual(job.progress["frames"]["percent"], 100)
        self.assertEqual(job.progress["frames"]["state"], "done")

    def test_skip_lines_do_not_start_channels(self):
        job = _both_job()
        webui.update_progress(job, "[watch] 3/4 转写：--no-transcribe，跳过")
        webui.update_progress(job, "[watch] 4/4 抽帧：--no-frames，跳过")
        for lane in ("transcript", "frames"):
            ch = job.progress[lane]
            self.assertEqual(ch["percent"], 0)
            self.assertEqual(ch["state"], "running")
            self.assertEqual(ch["stage"], "排队中")
        # C02：而「输入无视频流，自动跳过」是想抽但抽不了 → 标记 skipped（见 C02 测试）
        webui.update_progress(job, "[watch] 4/4 抽帧：输入无视频流（纯音频），自动跳过")
        self.assertEqual(job.progress["frames"]["state"], "skipped")

    def test_unselected_lane_stays_skipped(self):
        job = webui.Job("single", [URL], want_transcript=True, want_frames=False)
        self.assertEqual(job.progress["frames"]["state"], "skipped")
        self.assertEqual(job.progress["frames"]["stage"], "未选择")
        for line in ("[watch] ▶ 4/4 抽帧 (frames)", "[watch] ✅ 完成"):
            webui.update_progress(job, line)
        # 未选通道不被锚点驱动
        self.assertEqual(job.progress["frames"]["percent"], 0)
        self.assertEqual(job.progress["frames"]["state"], "skipped")
        # 已选通道正常推进
        self.assertEqual(job.progress["transcript"]["percent"], 100)

    def test_progress_percent_is_monotonic(self):
        job = _both_job()
        webui.update_progress(job, "[watch] 3/4 转写：使用平台字幕")
        webui.update_progress(job, "[watch] ▶ 1/4 探测输入 (probe)")  # 乱序锚点
        self.assertEqual(job.progress["transcript"]["percent"], 45)   # percent 不回退
        self.assertEqual(job.progress["transcript"]["stage"], "探测输入")  # stage 跟随锚点

    def test_frames_progress_line_does_not_touch_transcript(self):
        # 回归：`抽帧进度 2/40` 含子串 "2/4"，不得误命中文字稿下载锚点
        job = _both_job()
        webui.update_progress(job, "[watch] 3/4 转写：使用平台字幕")
        webui.update_progress(job, "  [frames] [video-watch] 抽帧进度 2/40")
        self.assertEqual(job.progress["transcript"]["percent"], 45)
        self.assertEqual(job.progress["transcript"]["stage"], "转写中")
        self.assertEqual(job.progress["frames"]["percent"], 12 + round(83 * 2 / 40))

    def test_result_not_ok_marks_channels_error(self):
        job = _both_job()
        webui.update_progress(job, "[watch] ▶ 1/4 探测输入 (probe)")
        webui._finalize_job_progress(job, "error")
        for lane in ("transcript", "frames"):
            self.assertEqual(job.progress[lane]["state"], "error")
            self.assertEqual(job.progress[lane]["stage"], "失败")

    def test_result_ok_finalizes_running_channels(self):
        job = _both_job()
        webui._finalize_job_progress(job, "done")
        for lane in ("transcript", "frames"):
            self.assertEqual(job.progress[lane]["state"], "done")
            self.assertEqual(job.progress[lane]["percent"], 100)

    def test_missing_result_marks_error(self):
        job = _both_job()
        webui._finalize_job_progress(job, "error")
        self.assertEqual(job.progress["transcript"]["state"], "error")

    def test_snapshot_carries_progress(self):
        job = webui.Job("single", [URL], True, False)
        snap = job.snapshot()
        self.assertIn("progress", snap)
        self.assertEqual(snap["progress"]["transcript"]["state"], "running")
        self.assertEqual(snap["progress"]["frames"]["state"], "skipped")

    def test_run_job_drives_progress_end_to_end(self):
        # 经 run_job（mock Popen）：日志锚点推进 → RESULT_JSON 收尾
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fj = Path(tmp.name) / "frames.json"
        fj.write_text('[{"file":"frame.jpg"}]', encoding="utf-8")
        output = "\n".join([
            "[watch] ▶ 1/4 探测输入 (probe)",
            "[watch] ▶ 4/4 抽帧 (frames)",
            "  [frames] [video-watch] 抽帧进度 10/10",
            "[watch] ✅ 完成",
            webui.RESULT_PREFIX + json.dumps({"ok": True, "run_dir": tmp.name,
                                             "frames_json":str(fj), "frame_count":1},
                                             ensure_ascii=False),
        ]) + "\n"
        job = webui.Job("single", [URL], False, True)   # 只选关键帧
        with mock.patch("subprocess.Popen", side_effect=_FakePopen(output)):
            webui.run_job(job)
        self.assertEqual(job.progress["frames"]["state"], "done")
        self.assertEqual(job.progress["frames"]["percent"], 100)
        self.assertEqual(job.progress["transcript"]["state"], "skipped")

    def test_run_job_marks_error_on_result_not_ok(self):
        output = ("[watch] ▶ 1/4 探测输入 (probe)\n"
                  + webui.RESULT_PREFIX + '{"ok": false, "error": "下载失败"}\n')
        job = _both_job()
        with mock.patch("subprocess.Popen",
                        side_effect=_FakePopen(output, returncode=1)):
            webui.run_job(job)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.progress["transcript"]["state"], "error")
        self.assertEqual(job.progress["frames"]["state"], "error")


# ---------------------------------------------------------------- 关键字定位关键帧


class KeywordLockTests(unittest.TestCase):
    def test_parse_keywords_separators(self):
        self.assertEqual(webui.parse_keywords("你好，world、hello  你好"),
                         ["你好", "world", "hello"])
        self.assertEqual(webui.parse_keywords(""), [])
        self.assertEqual(webui.parse_keywords("  "), [])

    def test_find_keyword_times_midpoint_and_case_insensitive(self):
        segs = [
            {"start": 10.0, "end": 14.0, "text": "你好世界"},
            {"start": 20.0, "end": 22.0, "text": "Hello WORLD"},
            {"start": 30.0, "end": 32.0, "text": "无关内容"},
        ]
        hits = webui.find_keyword_times(segs, ["你好", "world"])
        self.assertEqual(hits, [(12.0, "你好"), (21.0, "world")])

    def test_find_keyword_times_dedup_within_1s(self):
        segs = [
            {"start": 9.5, "end": 10.5, "text": "你好"},    # 中点 10.0
            {"start": 10.2, "end": 10.8, "text": "你好"},   # 中点 10.5 → <1s 去重
            {"start": 11.0, "end": 11.4, "text": "你好"},   # 中点 11.2 → 保留
        ]
        hits = webui.find_keyword_times(segs, ["你好"])
        self.assertEqual([t for t, _ in hits], [10.0, 11.2])

    def test_find_keyword_times_caps_at_60(self):
        segs = [{"start": float(i * 2), "end": float(i * 2) + 1.0, "text": "你好"}
                for i in range(100)]   # 中点间距 2s，共 100 命中
        hits = webui.find_keyword_times(segs, ["你好"])
        self.assertEqual(len(hits), webui.KEYWORD_MAX_HITS)
        self.assertEqual(hits[0][0], 0.5)      # 均匀抽稀保留首尾
        self.assertEqual(hits[-1][0], 198.5)

    def test_find_keyword_times_skips_malformed_segments(self):
        segs = [{"start": "x", "end": 2.0, "text": "你好"},
                {"text": "你好"},
                "不是字典",
                {"start": 1.0, "end": 3.0, "text": "你好"}]
        hits = webui.find_keyword_times(segs, ["你好"])
        self.assertEqual(hits, [(2.0, "你好")])

    def test_keyword_lock_end_to_end(self):
        """keyword_lock_frames：读 manifest+transcript → 写 keyword_times.json → 调 frames.py。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "v.mp4").write_bytes(b"fake")
            (run_dir / "manifest.json").write_text(
                json.dumps({"video_path": str(run_dir / "v.mp4")}), encoding="utf-8")
            (run_dir / "transcript.json").write_text(json.dumps(
                [{"start": 4.0, "end": 6.0, "text": "你好世界"}]), encoding="utf-8")
            job = webui.Job("single", [URL], True, True, keywords=["你好"])
            job.results = [{"ok": True, "run_dir": str(run_dir), "frames_json": None}]
            fake = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="[frames] 完成\n" + webui.RESULT_PREFIX + json.dumps(
                    {"ok": True, "added": 1, "count": 1,
                     "frames_json": str(run_dir / "frames" / "frames.json")},
                    ensure_ascii=False) + "\n",
                stderr="")
            with mock.patch("webui._run_cancellable_command", return_value=fake) as mrun:
                webui.keyword_lock_frames(job)
            self.assertEqual(job.results[0]["keyword"], {"matched": 1, "added": 1, "total_matched": 1})
            kw = json.loads((run_dir / "keyword_times.json").read_text(encoding="utf-8"))
            self.assertEqual(kw["version"], 1)
            self.assertEqual(kw["times"], [{"t": 5.0, "reason": "keyword: 你好"}])
            # frames.py 调用形态：--times-json --append --pass-id kw1
            cmd = mrun.call_args[0][1]
            self.assertTrue(any("frames.py" in str(c) for c in cmd))
            self.assertIn("--times-json", cmd)
            self.assertIn("--append", cmd)
            self.assertEqual(cmd[cmd.index("--pass-id") + 1], "kw1")

    def test_keyword_lock_zero_hits_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "v.mp4").write_bytes(b"fake")
            (run_dir / "manifest.json").write_text(
                json.dumps({"video_path": str(run_dir / "v.mp4")}), encoding="utf-8")
            (run_dir / "transcript.json").write_text(json.dumps(
                [{"start": 0.0, "end": 1.0, "text": "无关内容"}]), encoding="utf-8")
            job = webui.Job("single", [URL], True, True, keywords=["你好"])
            job.results = [{"ok": True, "run_dir": str(run_dir), "frames_json": None}]
            with mock.patch("subprocess.run") as mrun:   # 0 命中不应调 frames.py
                webui.keyword_lock_frames(job)
            self.assertEqual(job.results[0]["keyword"], {"matched": 0, "added": 0, "total_matched": 0})
            mrun.assert_not_called()
            self.assertFalse((run_dir / "keyword_times.json").exists())

    def test_keyword_lock_skips_when_manifest_or_transcript_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = webui.Job("single", [URL], True, True, keywords=["你好"])
            job.results = [{"ok": True, "run_dir": tmp, "frames_json": None}]
            webui.keyword_lock_frames(job)   # 不抛异常
            self.assertNotIn("keyword", job.results[0])


# ---------------------------------------------------------------- 关键帧去重


class DedupTests(unittest.TestCase):
    def test_ahash_identical_inputs_distance_zero(self):
        h1 = webui.ahash_from_gray(bytes([128] * 256))
        h2 = webui.ahash_from_gray(bytes([128] * 256))
        self.assertEqual(webui.hamming(h1, h2), 0)

    def test_ahash_bit_rule(self):
        # 均值 100：0 → bit 0，200 → bit 1 → 低 128 位全 1
        h = webui.ahash_from_gray(bytes([0] * 128 + [200] * 128))
        self.assertEqual(h, (1 << 128) - 1)
        self.assertEqual(webui.hamming(h, h), 0)
        self.assertEqual(webui.hamming(h, 0), 128)

    def test_ahash_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            webui.ahash_from_gray(b"\x00" * 100)

    def test_dedup_marks_dropped_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            frames_dir = run_dir / "frames"
            frames_dir.mkdir()
            for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
                (frames_dir / name).write_bytes(b"\xff\xd8fake")
            entries = [
                {"file": "a.jpg", "t": 0.0, "actual_t": 0.0, "source": "uniform"},
                {"file": "b.jpg", "t": 1.0, "actual_t": 1.0, "source": "uniform"},
                {"file": "c.jpg", "t": 2.0, "actual_t": 2.0, "source": "scene"},
                {"file": "d.jpg", "t": 3.0, "actual_t": 3.0, "source": "uniform"},
            ]
            fj = frames_dir / "frames.json"
            fj.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            gray_a = bytes([0] * 128 + [200] * 128)   # a、b 同 hash
            gray_b = bytes([200] * 128 + [0] * 128)   # 与 a 完全不同（汉明 256）
            gray_c = bytes(range(64)) * 4             # 另一种分布
            table = {"a.jpg": gray_a, "b.jpg": gray_a, "c.jpg": gray_b, "d.jpg": gray_c}
            job = webui.Job("single", [URL], True, True,
                            dedup={"enabled": True, "threshold": 0.95})
            job.results = [{"ok": True, "run_dir": str(run_dir),
                            "frames_json": str(fj), "frames_dir": str(frames_dir)}]
            webui.dedup_frames(job, extract_fn=lambda p: table[p.name])

            self.assertEqual(job.results[0]["dedup"],
                             {"total_count": 4, "kept_count": 3, "dropped_count": 1})
            data = json.loads(fj.read_text(encoding="utf-8"))
            b_entry = next(e for e in data if e["file"] == "b.jpg")
            self.assertTrue(b_entry["dropped"])
            self.assertEqual(b_entry["dedup_similarity"], 1.0)
            self.assertEqual(b_entry["source"], "uniform")   # 其他字段原样保留
            for kept_name in ("a.jpg", "c.jpg", "d.jpg"):
                self.assertNotIn("dropped",
                                 next(e for e in data if e["file"] == kept_name))
            report = json.loads((run_dir / "dedup_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["threshold"], 0.95)
            self.assertEqual(report["total"], 4)
            self.assertEqual(report["kept"], 3)
            self.assertEqual(report["dropped"],
                             [{"file": "b.jpg", "t": 1.0, "similarity": 1.0}])
            # 原子写无残留
            self.assertFalse((run_dir / "dedup_report.json.tmp").exists())
            self.assertFalse((frames_dir / "frames.json.tmp").exists())

    def test_dedup_extract_failure_keeps_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            frames_dir = run_dir / "frames"
            frames_dir.mkdir()
            entries = [{"file": "a.jpg", "t": 0.0, "actual_t": 0.0},
                       {"file": "b.jpg", "t": 1.0, "actual_t": 1.0}]
            fj = frames_dir / "frames.json"
            fj.write_text(json.dumps(entries), encoding="utf-8")
            job = webui.Job("single", [URL], True, True,
                            dedup={"enabled": True, "threshold": 0.95})
            job.results = [{"ok": True, "run_dir": str(run_dir),
                            "frames_json": str(fj)}]
            # 全部抽像素失败 → 全部保留、不剔除，且失败的帧计入 kept（C06）
            webui.dedup_frames(job, extract_fn=lambda p: None)
            self.assertEqual(job.results[0]["dedup"],
                             {"total_count": 2, "kept_count": 2, "dropped_count": 0})
            data = json.loads(fj.read_text(encoding="utf-8"))
            self.assertTrue(all("dropped" not in e for e in data))


# ---------------------------------------------------------------- C03 自定义间隔


class IntervalRuleTests(unittest.TestCase):
    def test_c03_interval_points_exact_quarters(self):
        # 20 秒视频、5 秒间隔 → 恰好 0/5/10/15 四帧
        self.assertEqual(webui.interval_points(20, 5), [0.0, 5.0, 10.0, 15.0])
        self.assertEqual(webui.interval_points(20.5, 5), [0.0, 5.0, 10.0, 15.0, 20.0])
        self.assertEqual(webui.interval_points(10, 0.5)[-1], 9.5)
        self.assertEqual(len(webui.interval_points(10, 0.5)), 20)
        self.assertEqual(webui.interval_points(0, 5), [])
        self.assertEqual(webui.interval_points(20, 0), [])

    def test_c03_count_rejects_float_and_bool(self):
        for bad in (1.8, True, 2.5):
            with self.subTest(value=bad):
                _, err = webui.validate_job_request(
                    {"mode": "single", "url": URL, "want_frames": True,
                     "frame_rule": {"type": "count", "value": bad}})
                self.assertIsNotNone(err, str(bad))

    def test_c03_run_interval_frames_writes_times_and_updates_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "v.mp4").write_bytes(b"fake")
            (run_dir / "manifest.json").write_text(json.dumps(
                {"duration": 20.0, "video_path": str(run_dir / "v.mp4")}),
                encoding="utf-8")
            job = webui.Job("single", [URL], True, True,
                            frame_rule={"type": "interval", "value": 5})
            entry = {"ok": True, "run_dir": str(run_dir)}
            captured = {}

            def fake_step(job_, cmd):
                captured["cmd"] = cmd
                times = json.loads((run_dir / "interval_times.json")
                                   .read_text(encoding="utf-8"))
                captured["times"] = times
                return {"ok": True, "count": 4,
                        "frames_json": str(run_dir / "frames" / "frames.json")}

            with mock.patch.object(webui, "_run_frames_step", side_effect=fake_step):
                webui.run_interval_frames(job, entry)
            self.assertEqual([p["t"] for p in captured["times"]["times"]],
                             [0.0, 5.0, 10.0, 15.0])
            self.assertEqual(captured["times"]["pass_id"], "base")
            cmd = captured["cmd"]
            self.assertTrue(any("frames.py" in str(c) for c in cmd))
            self.assertIn("--times-json", cmd)
            self.assertNotIn("--append", cmd)   # 基础轮
            self.assertEqual(cmd[cmd.index("--pass-id") + 1], "base")
            self.assertEqual(entry["frame_count"], 4)
            self.assertTrue(entry["frames_json"].endswith("frames.json"))
            self.assertNotIn("frames_error", entry)

    def test_c03_run_interval_frames_truncates_over_hard_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "v.mp4").write_bytes(b"fake")
            (run_dir / "manifest.json").write_text(json.dumps(
                {"duration": 600.0, "video_path": str(run_dir / "v.mp4")}),
                encoding="utf-8")
            job = webui.Job("single", [URL], True, True,
                            frame_rule={"type": "interval", "value": 0.5})
            entry = {"ok": True, "run_dir": str(run_dir)}
            captured = {}

            def fake_step(job_, cmd):
                captured["times"] = json.loads(
                    (run_dir / "interval_times.json").read_text(encoding="utf-8"))
                return {"ok": True, "count": 100, "frames_json": "x/frames.json"}

            with mock.patch.object(webui, "_run_frames_step", side_effect=fake_step):
                webui.run_interval_frames(job, entry)
            # 600s / 0.5s = 1200 点 → 截断到 100（2fps/100 硬上限）
            self.assertEqual(len(captured["times"]["times"]), 100)


# ---------------------------------------------------------------- C02 产物级状态


class ArtifactStatusTests(unittest.TestCase):
    def _job_with(self, results, want_transcript=True, want_frames=True):
        job = webui.Job("single", [URL], want_transcript, want_frames)
        job.results = results
        webui.annotate_artifact_status(job)
        return job

    def test_c02_succeeded_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            tt = Path(tmp) / "transcript.txt"
            fj = Path(tmp) / "frames" / "frames.json"
            tt.write_text("x", encoding="utf-8")
            fj.parent.mkdir()
            fj.write_text("[]", encoding="utf-8")
            r = self._job_with([{
                "ok": True, "run_dir": tmp, "transcript_txt": str(tt),
                "frames_json": str(fj), "frame_count": 3,
                "has_video": True, "has_audio": True,
            }]).results[0]
            self.assertEqual(r["artifacts"]["transcript"]["status"], "succeeded")
            self.assertEqual(r["artifacts"]["frames"]["status"], "succeeded")

    def test_c02_no_audio_transcript_skipped_with_reason(self):
        r = self._job_with([{
            "ok": True, "run_dir": "x", "transcript_txt": None,
            "transcript_source": "none", "has_audio": False, "has_video": True,
            "frames_json": None, "frame_count": None,
        }]).results[0]
        art = r["artifacts"]["transcript"]
        self.assertEqual(art["status"], "skipped")
        self.assertEqual(art["reason"], "无音轨")

    def test_c02_no_video_frames_skipped_with_reason(self):
        r = self._job_with([{
            "ok": True, "run_dir": "x", "transcript_txt": None,
            "transcript_source": "faster-whisper", "has_audio": True,
            "has_video": False, "frames_json": None, "frame_count": 0,
        }]).results[0]
        art = r["artifacts"]["frames"]
        self.assertEqual(art["status"], "skipped")
        self.assertEqual(art["reason"], "无视频流")

    def test_c02_empty_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fj = Path(tmp) / "frames" / "frames.json"
            fj.parent.mkdir()
            fj.write_text("[]", encoding="utf-8")
            r = self._job_with([{
                "ok": True, "run_dir": tmp, "transcript_txt": None,
                "transcript_source": "faster-whisper", "has_audio": True,
                "frames_json": str(fj), "frame_count": 0, "has_video": True,
            }]).results[0]
            self.assertEqual(r["artifacts"]["frames"]["status"], "empty")
            self.assertEqual(r["artifacts"]["transcript"]["status"], "failed")

    def test_c02_failed_result_marks_both_failed(self):
        r = self._job_with([{"ok": False, "error": "下载失败"}]).results[0]
        self.assertEqual(r["artifacts"]["transcript"]["status"], "failed")
        self.assertEqual(r["artifacts"]["frames"]["status"], "failed")

    def test_c02_unwanted_product_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tt = Path(tmp) / "transcript.txt"
            tt.write_text("x", encoding="utf-8")
            r = self._job_with([{
                "ok": True, "run_dir": tmp, "transcript_txt": str(tt),
                "has_audio": True,
            }], want_frames=False).results[0]
            self.assertEqual(r["artifacts"]["transcript"]["status"], "succeeded")
            self.assertEqual(r["artifacts"]["frames"]["status"], "skipped")
            self.assertEqual(r["artifacts"]["frames"]["reason"], "未选择该产物")

    def test_c02_frames_error_propagates(self):
        r = self._job_with([{
            "ok": True, "run_dir": "x", "has_video": True,
            "frames_error": "时长未知或视频缺失", "frames_json": None,
        }]).results[0]
        art = r["artifacts"]["frames"]
        self.assertEqual(art["status"], "failed")
        self.assertIn("时长未知", art["reason"])

    def test_c02_extract_results_redacts_url_credentials(self):
        # T05 并入：results 的 url 脱敏展示，不泄露查询串/凭据
        results = webui.extract_results(
            {"ok": True, "run_dir": "r", "title": "t"},
            "https://user:secret@example.com:8443/v?id=1&token=abc#frag")
        self.assertEqual(results[0]["url"], "https://example.com:8443/v")

    def test_c02_extract_results_carries_source_and_tracks(self):
        results = webui.extract_results({
            "ok": True, "run_dir": "r", "title": "t",
            "transcript_source": "none", "has_video": True, "has_audio": False,
        }, URL)
        r = results[0]
        self.assertEqual(r["transcript_source"], "none")
        self.assertTrue(r["has_video"])
        self.assertFalse(r["has_audio"])


# ---------------------------------------------------------------- C05 批次进度


class BatchProgressTests(unittest.TestCase):
    def test_c05_batch_first_item_done_is_not_100(self):
        job = webui.Job("batch", [URL, URL], True, False)
        webui.update_progress(job, "[watch] ▶ 1/4 探测输入 (probe)")
        webui.update_progress(job, "[watch] ✅ 完成")
        tr = job.progress["transcript"]
        self.assertEqual(tr["percent"], 50.0)     # (1 完成项 + 0)/2
        self.assertEqual(tr["state"], "running")  # 第一项完成不置 done
        # 模拟 run_job 进入第 2 项
        job.current_item = 2
        job.items_done = 1
        webui.update_progress(job, "[watch] ▶ 1/4 探测输入 (probe)")
        webui.update_progress(job, "[watch] ✅ 完成")
        self.assertEqual(tr["percent"], 100.0)
        self.assertEqual(tr["state"], "done")

    def test_c05_multi_episode_header_drives_total(self):
        job = webui.Job("multi", [URL], True, True, item="1-2")
        self.assertIsNone(job.items_total)   # multi 总数未知
        webui.update_progress(job, "[watch] ── 第 1 集（1/2）───")
        self.assertEqual(job.items_total, 2)
        webui.update_progress(job, "[watch] ▶ 1/4 探测输入 (probe)")
        # (items_done=0 + 5%)/2 = 2.5
        self.assertEqual(job.progress["transcript"]["percent"], 2.5)
        webui.update_progress(job, "[watch] ✅ 完成")
        self.assertEqual(job.progress["transcript"]["percent"], 50.0)
        self.assertEqual(job.progress["transcript"]["state"], "running")
        webui.update_progress(job, "[watch] ── 第 2 集（2/2）───")
        self.assertEqual(job.progress["transcript"]["intra"], 0.0)

    def test_c05_frames_waits_for_postprocess(self):
        job = webui.Job("single", [URL], True, True, keywords=["你好"])
        self.assertTrue(job.frames_postprocess_pending())
        webui.update_progress(job, "[watch] ▶ 4/4 抽帧 (frames)")
        webui.update_progress(job, "[watch] ✅ 完成")
        fr = job.progress["frames"]
        self.assertEqual(fr["state"], "running")   # 有后处理不置 done
        self.assertEqual(job.progress["transcript"]["state"], "done")  # 文字稿无后处理
        webui._finalize_job_progress(job, "done")
        self.assertEqual(fr["state"], "done")
        self.assertEqual(fr["percent"], 100.0)

    def test_c02_lane_marks_skipped_for_missing_tracks(self):
        # C02：想转写但无音轨 / 想抽帧但无视频流 → 通道"已跳过"而非 100%
        job = _both_job()
        webui.update_progress(job, "[watch] 3/4 转写：无音频流且无可用字幕，source=none")
        tr = job.progress["transcript"]
        self.assertEqual(tr["state"], "skipped")
        self.assertEqual(tr["stage"], "已跳过：无音轨")
        job2 = _both_job()
        webui.update_progress(job2, "[watch] 4/4 抽帧：输入无视频流（纯音频），自动跳过")
        fr = job2.progress["frames"]
        self.assertEqual(fr["state"], "skipped")
        self.assertEqual(fr["stage"], "已跳过：无视频流")
        # --no-transcribe/--no-frames 的主动跳过行不误标（通道本就在跑另说）
        job3 = _both_job()
        webui.update_progress(job3, "[watch] 3/4 转写：--no-transcribe，跳过")
        self.assertEqual(job3.progress["transcript"]["state"], "running")
        webui.update_progress(job3, "[watch] 4/4 抽帧：--no-frames，跳过")
        self.assertEqual(job3.progress["frames"]["state"], "running")


# ---------------------------------------------------------------- C06/C07 副本与 manifest 同步


class KeptCopyAndManifestTests(unittest.TestCase):
    def test_c06_friendly_copies_only_kept_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            frames_dir = run_dir / "frames"
            frames_dir.mkdir()
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                (frames_dir / name).write_bytes(b"\xff\xd8" + name.encode())
            (frames_dir / "frames.json").write_text(json.dumps([
                {"file": "a.jpg", "t": 0.0},
                {"file": "b.jpg", "t": 1.0, "dropped": True, "dedup_similarity": 0.99},
                {"file": "c.jpg", "t": 2.0},
            ]), encoding="utf-8")
            job = webui.Job("single", [URL], True, True)
            job.results = [{"ok": True, "run_dir": str(run_dir),
                            "frames_dir": str(frames_dir),
                            "frames_json": str(frames_dir / "frames.json")}]
            webui.friendly_outputs(job)
            copied = sorted(p.name for p in (run_dir / "关键帧").iterdir())
            self.assertEqual(copied, ["a.jpg", "c.jpg"])   # dropped 帧不进副本
            self.assertEqual(job.results[0]["friendly"]["frame_count"], 2)

    def test_c07_manifest_sync_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "manifest.json").write_text(json.dumps({
                "frames": {"count": 2, "base_count": 2,
                           "passes": [{"pass_id": "base", "count": 2}]},
            }), encoding="utf-8")
            result = {"ok": True, "added": 1, "count": 3}
            webui._sync_manifest_keyword(run_dir, result, ["你好"])
            # 重跑相同参数：added=0、count 不变（frames.py 时间去重）
            webui._sync_manifest_keyword(run_dir, {"ok": True, "added": 0, "count": 3},
                                         ["你好"])
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["frames"]["count"], 3)
            kw_passes = [p for p in manifest["frames"]["passes"]
                         if p.get("pass_id") == "kw1"]
            self.assertEqual(len(kw_passes), 1)          # upsert 不重复
            self.assertEqual(kw_passes[0]["count"], 0)   # 重跑如实记录 added=0
            self.assertEqual(kw_passes[0]["keyword"], "你好")
            self.assertEqual(manifest["frames"]["base_count"], 2)   # base 轮不动


# ---------------------------------------------------------------- 中文命名副本


class FriendlyOutputsTests(unittest.TestCase):
    """friendly_outputs：在 run_dir 生成 文字稿.txt/.srt 与 关键帧/ 副本。"""

    def _make_run(self, root):
        run_dir = root / "测试视频_20260101"
        (run_dir / "frames").mkdir(parents=True)
        (run_dir / "transcript.txt").write_text("[00:01] 你好", encoding="utf-8")
        (run_dir / "transcript.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n你好",
                                                encoding="utf-8")
        (run_dir / "frames" / "0001_t0001.0.jpg").write_bytes(b"\xff\xd8fake1")
        (run_dir / "frames" / "0002_t0002.0.jpg").write_bytes(b"\xff\xd8fake2")
        (run_dir / "frames" / "frames.json").write_text("[]", encoding="utf-8")
        return run_dir

    def test_creates_chinese_named_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(Path(tmp))
            job = webui.Job("single", [URL], True, True)
            job.results = [{
                "url": URL, "ok": True, "run_dir": str(run_dir),
                "transcript_txt": str(run_dir / "transcript.txt"),
                "frames_dir": str(run_dir / "frames"),
            }]
            webui.friendly_outputs(job)
            # 中文副本就位且内容一致
            self.assertEqual((run_dir / "文字稿.txt").read_text(encoding="utf-8"),
                             "[00:01] 你好")
            self.assertTrue((run_dir / "文字稿.srt").is_file())
            cn_frames = run_dir / "关键帧"
            self.assertEqual(sorted(p.name for p in cn_frames.iterdir()),
                             ["0001_t0001.0.jpg", "0002_t0002.0.jpg"])
            # 原文件保留不动
            self.assertTrue((run_dir / "transcript.txt").is_file())
            self.assertTrue((run_dir / "frames" / "frames.json").is_file())
            friendly = job.results[0]["friendly"]
            self.assertEqual(friendly["frame_count"], 2)
            self.assertTrue(friendly["frames_dir"].endswith("关键帧"))

    def test_skips_failed_or_missing_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = webui.Job("batch", [URL, URL], True, True)
            job.results = [
                {"url": URL, "ok": False, "run_dir": None, "error": "下载失败"},
                {"url": URL, "ok": True, "run_dir": str(Path(tmp) / "不存在"),
                 "transcript_txt": str(Path(tmp) / "不存在" / "transcript.txt"),
                 "frames_dir": None},
            ]
            webui.friendly_outputs(job)  # 不抛异常
            self.assertNotIn("friendly", job.results[0])
            self.assertNotIn("friendly", job.results[1])


# ---------------------------------------------------------------- 请求校验


class ValidateRequestTests(unittest.TestCase):
    def test_rejects_unknown_mode(self):
        _, err = webui.validate_job_request({"mode": "x", "url": URL,
                                             "want_transcript": True})
        self.assertIsNotNone(err)

    def test_rejects_empty_url(self):
        _, err = webui.validate_job_request({"mode": "single", "url": "  ",
                                             "want_frames": True})
        self.assertIsNotNone(err)

    def test_rejects_no_product_selected(self):
        _, err = webui.validate_job_request({"mode": "single", "url": URL,
                                             "want_transcript": False,
                                             "want_frames": False})
        self.assertIsNotNone(err)

    def test_rejects_empty_batch_urls(self):
        _, err = webui.validate_job_request({"mode": "batch", "urls": ["", "  "],
                                             "want_frames": True})
        self.assertIsNotNone(err)

    def test_rejects_bad_item_spec(self):
        for bad in ("0", "7-3", "abc", "3-"):
            with self.subTest(item=bad):
                _, err = webui.validate_job_request(
                    {"mode": "multi", "url": URL, "item": bad,
                     "want_transcript": True})
                self.assertIsNotNone(err, bad)

    def test_multi_defaults_item_to_all(self):
        params, err = webui.validate_job_request(
            {"mode": "multi", "url": URL, "item": "", "want_transcript": True})
        self.assertIsNone(err)
        self.assertEqual(params["item"], "all")

    def test_batch_strips_blank_lines(self):
        params, err = webui.validate_job_request(
            {"mode": "batch", "urls": [URL, " ", "https://a.example/2"],
             "want_frames": True})
        self.assertIsNone(err)
        self.assertEqual(params["urls"], [URL, "https://a.example/2"])

    def test_u06_batch_accepts_per_url_item(self):
        params, err = webui.validate_job_request(
            {"mode": "batch",
             "urls": [{"url": URL, "item": "1-3"}, {"url": URL, "item": "5"},
                      {"url": URL}],
             "want_frames": True})
        self.assertIsNone(err)
        self.assertEqual(params["urls"],
                         [{"url": URL, "item": "1-3"},
                          {"url": URL, "item": "5"},
                          {"url": URL, "item": None}])

    def test_u06_batch_rejects_bad_per_url_item(self):
        _, err = webui.validate_job_request(
            {"mode": "batch", "urls": [{"url": URL, "item": "7-3"}],
             "want_frames": True})
        self.assertIsNotNone(err)

    # ---- 侧栏三选项校验 ----

    def test_frame_rule_count_and_interval_accepted(self):
        params, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_frames": True,
             "frame_rule": {"type": "count", "value": 30}})
        self.assertIsNone(err)
        self.assertEqual(params["frame_rule"], {"type": "count", "value": 30})
        params, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_frames": True,
             "frame_rule": {"type": "interval", "value": 5}})
        self.assertIsNone(err)
        self.assertEqual(params["frame_rule"], {"type": "interval", "value": 5.0})

    def test_frame_rule_invalid_rejected(self):
        for bad in ({"type": "count", "value": 0}, {"type": "count", "value": 101},
                    {"type": "count", "value": "abc"}, {"type": "interval", "value": 0.1},
                    {"type": "interval", "value": 601}, {"type": "bogus"}, "not-a-dict"):
            with self.subTest(rule=bad):
                _, err = webui.validate_job_request(
                    {"mode": "single", "url": URL, "want_frames": True,
                     "frame_rule": bad})
                self.assertIsNotNone(err, str(bad))

    def test_keyword_too_long_rejected(self):
        _, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_transcript": True,
             "keyword": "x" * 101})
        self.assertIsNotNone(err)

    def test_keyword_auto_enables_transcript(self):
        # 关键字定位依赖文字稿：want_transcript=false 时服务端自动改为 true
        params, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_frames": True,
             "want_transcript": False, "keyword": "你好 world"})
        self.assertIsNone(err)
        self.assertTrue(params["want_transcript"])
        self.assertEqual(params["keywords"], ["你好", "world"])

    def test_t05_strict_types(self):
        # T05：bool 必须 bool、整数拒绝 bool/小数、字符串拒绝非字符串、列表元素受检
        bad_payloads = [
            {"mode": "single", "url": URL, "want_transcript": "yes"},
            {"mode": "single", "url": URL, "want_frames": 1},
            {"mode": "single", "url": 123, "want_frames": True},
            {"mode": "batch", "urls": [123], "want_frames": True},
            {"mode": "batch", "urls": [{"url": 123}], "want_frames": True},
            {"mode": "single", "url": URL, "want_frames": True, "keyword": 123},
            {"mode": "multi", "url": URL, "item": 3, "want_frames": True},
            {"mode": "single", "url": URL, "want_frames": True,
             "dedup": {"enabled": "true"}},
            {"mode": "single", "url": URL, "want_frames": True,
             "dedup": {"enabled": True, "threshold": True}},
            {"mode": "single", "url": URL, "want_frames": True,
             "frame_rule": {"type": "interval", "value": True}},
        ]
        for p in bad_payloads:
            with self.subTest(payload=p):
                params, err = webui.validate_job_request(p)
                self.assertIsNotNone(err, str(p))
                self.assertIsNone(params)

    def test_t05_result_error_is_redacted(self):
        results = webui.extract_results(
            {"ok": False,
             "error": "下载失败 https://user:secret@example.com/v?token=abc"},
            URL)
        self.assertNotIn("secret", results[0]["error"])
        self.assertNotIn("token=abc", results[0]["error"])

    def test_dedup_threshold_range_and_default(self):
        for bad in (0.5, 1.0, "abc"):
            with self.subTest(threshold=bad):
                _, err = webui.validate_job_request(
                    {"mode": "single", "url": URL, "want_frames": True,
                     "dedup": {"enabled": True, "threshold": bad}})
                self.assertIsNotNone(err, str(bad))
        params, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_frames": True,
             "dedup": {"enabled": True, "threshold": 0.9}})
        self.assertIsNone(err)
        self.assertEqual(params["dedup"], {"enabled": True, "threshold": 0.9})
        params, err = webui.validate_job_request(
            {"mode": "single", "url": URL, "want_frames": True})
        self.assertIsNone(err)
        self.assertEqual(params["dedup"], {"enabled": False, "threshold": 0.95})


# ---------------------------------------------------------------- 路径安全


class PathSafetyTests(unittest.TestCase):
    def test_traversal_rejected_and_inside_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            inside = root / "job1" / "transcript.txt"
            inside.parent.mkdir(parents=True)
            inside.write_text("hi", encoding="utf-8")
            with mock.patch.object(webui, "RUNS_ROOT", root):
                self.assertEqual(webui.resolve_runs_path(str(inside)),
                                 inside.resolve())
                # 各种越界写法一律 None
                self.assertIsNone(webui.resolve_runs_path(str(root / ".." / "secret.txt")))
                self.assertIsNone(webui.resolve_runs_path(".."))
                self.assertIsNone(webui.resolve_runs_path(str(Path(tmp) / "other" / "f.txt")))
                self.assertIsNone(webui.resolve_runs_path(str(Path(tmp).parent)))


class _GatedPopen:
    """阻塞型假 Popen：迭代在 gate 打开前阻塞，模拟可取消的长任务。"""

    def __init__(self, gate):
        self._gate = gate
        self.returncode = 1
        self.pid = 43210
        self.stdout = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdout = self
        return self

    def __iter__(self):
        yield "[watch] 1/4 探测输入\n"
        self._gate.wait(10)
        yield webui.RESULT_PREFIX + json.dumps({"ok": False, "error": "已中止"}) + "\n"

    def wait(self, timeout=None):
        return self.returncode


class DedupRestoreTests(unittest.TestCase):
    def _make(self, tmp):
        run_dir = Path(tmp)
        frames = run_dir / "frames"
        frames.mkdir(parents=True)
        for n in ("a.jpg", "b.jpg"):
            (frames / n).write_bytes(b"\xff\xd8")
        (frames / "frames.json").write_text(json.dumps([
            {"file": "a.jpg", "t": 0.0},
            {"file": "b.jpg", "t": 1.0, "dropped": True, "dedup_similarity": 0.99},
        ]), encoding="utf-8")
        (run_dir / "dedup_report.json").write_text(json.dumps({
            "threshold": 0.95, "total": 2, "kept": 1,
            "dropped": [{"file": "b.jpg", "t": 1.0, "similarity": 0.99}]}),
            encoding="utf-8")
        return run_dir

    def test_restore_removes_flag_syncs_report_and_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make(tmp)
            (run_dir / "关键帧").mkdir()
            res = webui.dedup_restore(run_dir, "b.jpg")
            self.assertTrue(res["ok"])
            self.assertEqual((res["kept_count"], res["dropped_count"]), (2, 0))
            entries = json.loads((run_dir / "frames" / "frames.json")
                                 .read_text(encoding="utf-8"))
            b = next(e for e in entries if e["file"] == "b.jpg")
            self.assertNotIn("dropped", b)
            self.assertNotIn("dedup_similarity", b)
            report = json.loads((run_dir / "dedup_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["dropped"], [])
            self.assertEqual(report["kept"], 2)
            self.assertTrue((run_dir / "关键帧" / "b.jpg").is_file())
            # 幂等：重复恢复仍 ok，计数不变
            res2 = webui.dedup_restore(run_dir, "b.jpg")
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["kept_count"], 2)
            self.assertFalse((run_dir / "frames" / "frames.json.tmp").exists())

    def test_restore_missing_frame_or_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make(tmp)
            self.assertFalse(webui.dedup_restore(run_dir, "nope.jpg")["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(webui.dedup_restore(Path(tmp), "a.jpg")["ok"])


# ---------------------------------------------------------------- HTTP 端到端


class _FakePopen:
    """顶替 subprocess.Popen：喂固定 stdout（末行 RESULT_JSON），立即退出。"""

    def __init__(self, output, returncode=0):
        self._output = output
        self.returncode = returncode
        self.stdout = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdout = io.StringIO(self._output)
        return self

    def wait(self, timeout=None):
        return self.returncode


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.WebUIHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=15)

    def setUp(self):
        with webui.JOBS_LOCK:
            webui.JOBS.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fixture_root = Path(tmp.name)
        self.fixture_frames = self.fixture_root / "frames"
        self.fixture_frames.mkdir()
        (self.fixture_root / "transcript.txt").write_text("test speech", encoding="utf-8")
        (self.fixture_frames / "frames.json").write_text(
            json.dumps([{"file":f"{i}.jpg"} for i in range(3)]), encoding="utf-8")
        for i in range(3):
            (self.fixture_frames / f"{i}.jpg").write_bytes(b"image")

    def _success_result(self, **changes):
        """成功RESULT必须有真实产物，避免只凭ok:true掩盖丢失文件。"""
        result = {"ok":True, "title":"t", "run_dir":str(self.fixture_root),
                  "transcript_txt":str(self.fixture_root / "transcript.txt"),
                  "frames_dir":str(self.fixture_frames),
                  "frames_json":str(self.fixture_frames / "frames.json"), "frame_count":3}
        result.update(changes)
        return result

    def _get(self, path):
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}{path}", timeout=15) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
            except (ConnectionError, TimeoutError):
                # Windows loopback 高频短连接偶发 RST/abort：测试客户端重试
                if attempt == 2:
                    raise
                time.sleep(0.3)

    def _get_json(self, path):
        status, body = self._get(path)
        return status, json.loads(body.decode("utf-8"))

    def _post_json(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))
            except (ConnectionError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(0.3)

    def test_get_index_returns_200_with_tabs(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("单个视频", body.decode("utf-8"))

    def test_health_returns_json(self):
        status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("missing", data)
        self.assertIn("gpu", data)

    def test_files_traversal_returns_403(self):
        # 与 cwd 无关：把 RUNS_ROOT 钉到临时目录，请求其外的路径必须 403
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            root.mkdir()
            outside = (Path(tmp) / "secret.txt").resolve()
            outside.write_text("x", encoding="utf-8")
            with mock.patch.object(webui, "RUNS_ROOT", root):
                for raw in (str(outside), str(root / ".." / "secret.txt"), ".."):
                    with self.subTest(path=raw):
                        q = urllib.parse.quote(raw)
                        status, data = self._get_json(f"/api/files?path={q}")
                        self.assertEqual(status, 403)
                        self.assertFalse(data["ok"])
                # 根内文件正常返回
                inside = root / "note.txt"
                inside.write_text("你好", encoding="utf-8")
                q = urllib.parse.quote(str(inside))
                status, body = self._get(f"/api/files?path={q}")
                self.assertEqual(status, 200)
                self.assertEqual(body.decode("utf-8"), "你好")

    def test_post_invalid_request_returns_400(self):
        status, data = self._post_json("/api/jobs", {"mode": "single", "url": "",
                                                     "want_frames": True})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_job_lifecycle_with_fake_popen(self):
        result_line = webui.RESULT_PREFIX + json.dumps(self._success_result(title="测试视频"), ensure_ascii=False)
        fake = _FakePopen("[watch] 1/4 探测输入\n[watch] ✅ 完成\n" + result_line + "\n")
        with mock.patch("subprocess.Popen", side_effect=fake):
            status, data = self._post_json("/api/jobs", {
                "mode": "single", "url": URL,
                "want_transcript": True, "want_frames": True,
            })
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            job_id = data["job_id"]
            # 轮询直到任务结束（mock 子进程应立即完成）
            deadline = time.time() + 15
            info = None
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertIsNotNone(info)
        self.assertEqual(info["status"], "done")
        self.assertEqual(len(info["results"]), 1)
        r = info["results"][0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "测试视频")
        self.assertEqual(r["frame_count"], 3)
        self.assertTrue(any("探测输入" in line for line in info["log"]))
        # 双通道进度随任务完成置 done
        self.assertEqual(info["progress"]["transcript"]["state"], "done")
        self.assertEqual(info["progress"]["frames"]["state"], "done")
        # 子进程以 list 形式调用且带 --no-review
        self.assertIn("--no-review", fake.cmd)
        self.assertNotIn("--item", fake.cmd)

    def test_job_multi_aggregate_lifecycle(self):
        aggregate = webui.RESULT_PREFIX + json.dumps({
            "ok": True, "succeeded": 1, "failed": 1, "total": 2,
            "episodes": [
                self._success_result(item=1),
                {"item": 2, "ok": False, "run_dir": "runs/course_p02",
                 "transcript_txt": None, "frames_json": None,
                 "error": "第 2 集模拟失败"},
            ],
        }, ensure_ascii=False)
        fake = _FakePopen("[watch] ═══ 多集模式 ═══\n" + aggregate + "\n")
        with mock.patch("subprocess.Popen", side_effect=fake):
            status, data = self._post_json("/api/jobs", {
                "mode": "multi", "url": URL, "item": "1-2",
                "want_transcript": True, "want_frames": False,
            })
            self.assertEqual(status, 200)
            job_id = data["job_id"]
            deadline = time.time() + 15
            info = None
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(info["status"], "partial")   # C02/C05：部分失败如实反映
        self.assertEqual(len(info["results"]), 2)
        self.assertTrue(info["results"][0]["ok"])
        self.assertFalse(info["results"][1]["ok"])
        self.assertEqual(info["results"][1]["error"], "第 2 集模拟失败")
        # multi 模式透传 --item，且只要文字稿时带 --no-frames
        self.assertEqual(fake.cmd[fake.cmd.index("--item") + 1], "1-2")
        self.assertIn("--no-frames", fake.cmd)
        # 未选通道 skipped，已选通道有失败 → error（部分失败）
        self.assertEqual(info["progress"]["transcript"]["state"], "error")
        self.assertEqual(info["progress"]["transcript"]["stage"], "部分失败")
        self.assertEqual(info["progress"]["transcript"]["percent"], 50)
        self.assertEqual(info["progress"]["frames"]["state"], "skipped")

    def test_job_with_sidebar_options_end_to_end(self):
        """POST /api/jobs 带 frame_rule + keyword + dedup 走通：mock watch.py 与 frames.py。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            frames_dir = run_dir / "frames"
            frames_dir.mkdir(parents=True)
            (run_dir / "v.mp4").write_bytes(b"fake")   # 关键字锁帧要求 video_path 存在
            (run_dir / "manifest.json").write_text(json.dumps(
                {"video_path": str(run_dir / "v.mp4")}), encoding="utf-8")
            (run_dir / "transcript.json").write_text(json.dumps(
                [{"start": 4.0, "end": 6.0, "text": "你好世界"}]), encoding="utf-8")
            (run_dir / "transcript.txt").write_text("你好世界", encoding="utf-8")
            (frames_dir / "frames.json").write_text(json.dumps(
                [{"file": "x.jpg", "t": 0.0, "actual_t": 0.0, "width": 512}]),
                encoding="utf-8")
            watch_result = webui.RESULT_PREFIX + json.dumps({
                "ok": True, "run_dir": str(run_dir), "title": "测试",
                "transcript_txt": str(run_dir / "transcript.txt"),
                "frames_dir": str(frames_dir),
                "frames_json": str(frames_dir / "frames.json"),
                "frame_count": 1,
            }, ensure_ascii=False)
            fake_popen = _FakePopen("[watch] 1/4 探测输入\n[watch] ✅ 完成\n"
                                    + watch_result + "\n")
            frames_result = webui.RESULT_PREFIX + json.dumps(
                {"ok": True, "added": 1, "count": 2,
                 "frames_json": str(frames_dir / "frames.json")}, ensure_ascii=False)
            fake_run = mock.Mock(return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="[frames] 完成\n" + frames_result + "\n", stderr=""))
            with mock.patch("subprocess.Popen", side_effect=fake_popen), \
                 mock.patch("subprocess.run", fake_run), \
                 mock.patch("webui._run_cancellable_command", side_effect=lambda job, cmd, **kw: fake_run(cmd, **kw)):
                status, data = self._post_json("/api/jobs", {
                    "mode": "single", "url": URL,
                    "want_transcript": True, "want_frames": True,
                    "frame_rule": {"type": "count", "value": 30},
                    "keyword": "你好",
                    "dedup": {"enabled": True, "threshold": 0.95},
                })
                self.assertEqual(status, 200)
                job_id = data["job_id"]
                deadline = time.time() + 15
                info = None
                while time.time() < deadline:
                    _, info = self._get_json(f"/api/jobs/{job_id}")
                    if info["status"] != "running":
                        break
                    time.sleep(0.05)
            self.assertIsNotNone(info)
            self.assertEqual(info["status"], "done")
            r = info["results"][0]
            # frame_rule count → watch.py 带 --budget 30（C03 目标帧数语义）
            self.assertEqual(
                fake_popen.cmd[fake_popen.cmd.index("--budget") + 1], "30")
            # 关键字锁帧：命中 1 处（中点 5.0）、新增 1 帧；keyword_times.json 落盘
            self.assertEqual(r["keyword"], {"matched": 1, "added": 1, "total_matched": 1})
            kw = json.loads((run_dir / "keyword_times.json").read_text(encoding="utf-8"))
            self.assertEqual(kw["times"], [{"t": 5.0, "reason": "keyword: 你好"}])
            # subprocess.run 的最后一次调用是去重的 ffmpeg 抽像素，frames.py 调用在历史里找
            calls = [c[0][0] for c in fake_run.call_args_list]
            fcmd = next(c for c in calls if any("frames.py" in str(x) for x in c))
            self.assertIn("--times-json", fcmd)
            self.assertIn("--append", fcmd)
            self.assertEqual(fcmd[fcmd.index("--pass-id") + 1], "kw1")
            # 去重跑通（mock 的 subprocess.run 输出非 256 字节 → 保留不剔除，计入 kept）
            self.assertEqual(r["dedup"], {"total_count": 1, "kept_count": 1,
                                          "dropped_count": 0})

    def test_batch_partial_status_end_to_end(self):
        """C02/C05：批量 2 成功 1 失败 → 整体 partial，不再被任一成功吞掉。"""
        ok_line = webui.RESULT_PREFIX + json.dumps(
            self._success_result(), ensure_ascii=False)
        bad_line = webui.RESULT_PREFIX + json.dumps(
            {"ok": False, "error": "下载失败"}, ensure_ascii=False)
        outputs = iter(["[watch] 1/4 探测输入\n[watch] ✅ 完成\n" + ok_line + "\n",
                        "[watch] 1/4 探测输入\n" + bad_line + "\n"])

        def factory(cmd, **kwargs):
            return _FakePopen(next(outputs))(cmd)

        with mock.patch("subprocess.Popen", side_effect=factory):
            status, data = self._post_json("/api/jobs", {
                "mode": "batch", "urls": [URL, "https://a.example/2"],
                "want_transcript": True, "want_frames": True,
            })
            self.assertEqual(status, 200)
            job_id = data["job_id"]
            deadline = time.time() + 15
            info = None
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(info["status"], "partial")
        self.assertEqual(len(info["results"]), 2)
        self.assertTrue(info["results"][0]["ok"])
        self.assertFalse(info["results"][1]["ok"])
        # 失败项的产物状态也已标注
        self.assertEqual(info["results"][1]["artifacts"]["transcript"]["status"],
                         "failed")

    # ---- POST /api/probe 与 /api/check_path（U05/U06）----

    def test_u06_probe_success_with_mocked_subprocess(self):
        payload = {"ok": True, "kind": "url", "title": "测试合集", "duration": 100.0,
                   "has_audio": True, "has_video": True,
                   "playlist": {"count": 2, "items": [
                       {"index": 1, "title": "第1集", "duration": 100.0},
                       {"index": 2, "title": "第2集", "duration": 105.0}]}}
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="[probe] yt_dlp 读取 URL 元数据\n" + webui.RESULT_PREFIX
                   + json.dumps(payload, ensure_ascii=False) + "\n",
            stderr="")
        with mock.patch("subprocess.run", return_value=fake) as mrun:
            status, data = self._post_json("/api/probe", {"input": URL})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["playlist"]["count"], 2)
        cmd = mrun.call_args[0][0]
        self.assertIsInstance(cmd, list)
        self.assertTrue(any("probe.py" in str(c) for c in cmd))
        self.assertIn("--input", cmd)

    def test_u06_probe_rejects_empty_input(self):
        status, data = self._post_json("/api/probe", {"input": "  "})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_u06_probe_failure_and_timeout_propagate(self):
        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout=webui.RESULT_PREFIX
                   + json.dumps({"ok": False, "error": "yt_dlp 元数据获取失败"},
                                ensure_ascii=False) + "\n",
            stderr="ERROR: ...")
        with mock.patch("subprocess.run", return_value=fail_result):
            status, data = self._post_json("/api/probe", {"input": URL})
        self.assertEqual(status, 500)
        self.assertFalse(data["ok"])
        self.assertIn("yt_dlp", data["error"])
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="probe", timeout=60)):
            status, data = self._post_json("/api/probe", {"input": URL})
        self.assertEqual(status, 500)
        self.assertFalse(data["ok"])
        self.assertIn("超时", data["error"])

    def test_u05_check_path_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "v.mp4"
            f.write_bytes(b"x")
            status, data = self._post_json("/api/check_path", {"path": str(f)})
            self.assertEqual((status, data["exists"], data["kind"]), (200, True, "file"))
            status, data = self._post_json("/api/check_path", {"path": tmp})
            self.assertEqual((data["exists"], data["kind"]), (True, "dir"))
            status, data = self._post_json(
                "/api/check_path", {"path": str(Path(tmp) / "不存在.mp4")})
            self.assertEqual((data["exists"], data["kind"]), (False, None))
            # Windows 引号清理
            status, data = self._post_json("/api/check_path", {"path": f'"{f}"'})
            self.assertEqual((data["exists"], data["kind"]), (True, "file"))
            status, data = self._post_json("/api/check_path", {"path": "  "})
            self.assertEqual(status, 400)

    def test_u06_batch_per_url_item_commands(self):
        """U06：批量逐条选集 → 每个子进程按条目带 --item。"""
        cmds = []

        def factory(cmd, **kwargs):
            cmds.append(cmd)
            out = webui.RESULT_PREFIX + json.dumps(
                self._success_result(), ensure_ascii=False)
            return _FakePopen("[watch] ✅ 完成\n" + out + "\n")(cmd)

        with mock.patch("subprocess.Popen", side_effect=factory):
            status, data = self._post_json("/api/jobs", {
                "mode": "batch",
                "urls": [{"url": URL, "item": "1-3"}, {"url": URL, "item": "5"}],
                "want_transcript": True, "want_frames": False,
            })
            self.assertEqual(status, 200)
            job_id = data["job_id"]
            deadline = time.time() + 15
            info = None
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(info["status"], "done")
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0][cmds[0].index("--item") + 1], "1-3")
        self.assertEqual(cmds[1][cmds[1].index("--item") + 1], "5")

    def test_u09_health_has_capabilities(self):
        status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        caps = data["capabilities"]
        for key in ("frames_ready", "transcribe_ready", "gpu_detected",
                    "cuda_libs_installed", "models_cached"):
            self.assertIn(key, caps)

    # ---- T01 持久化 / T02 取消 / T03 复用 / U08 恢复 ----

    def test_t01_persist_list_and_db_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            old = webui.STORE
            webui.STORE = store
            try:
                out = webui.RESULT_PREFIX + json.dumps(
                    self._success_result(), ensure_ascii=False)
                with mock.patch("subprocess.Popen",
                                side_effect=_FakePopen("[watch] ✅ 完成\n" + out + "\n")):
                    status, data = self._post_json("/api/jobs", {
                        "mode": "single", "url": URL,
                        "want_transcript": True, "want_frames": False})
                    self.assertEqual(status, 200)
                    job_id = data["job_id"]
                    deadline = time.time() + 15
                    info = None
                    while time.time() < deadline:
                        _, info = self._get_json(f"/api/jobs/{job_id}")
                        if info["status"] not in ("queued", "running"):
                            break
                        time.sleep(0.05)
                self.assertEqual(info["status"], "done")
                row = store.get(job_id)
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["params"]["mode"], "single")
                self.assertEqual(len(row["results"]), 1)
                _, listing = self._get_json("/api/jobs?limit=5")
                ids = [j["job_id"] for j in listing["jobs"]]
                self.assertIn(job_id, ids)
                # 内存清空后详情走库（重启语义）
                with webui.JOBS_LOCK:
                    webui.JOBS.clear()
                _, info2 = self._get_json(f"/api/jobs/{job_id}")
                self.assertEqual(info2["status"], "done")
                self.assertEqual(len(info2["results"]), 1)
            finally:
                webui.STORE = old

    def test_t01_interrupted_row_visible_in_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            store.upsert({
                "job_id": "hist1", "mode": "single",
                "params": json.dumps({"mode": "single"}), "status": "running",
                "progress": json.dumps({}), "results": json.dumps([]),
                "logs": json.dumps(["[watch] x"]), "error": None,
                "media_key": None, "created_at": "2026-01-01T00:00:00",
                "finished_at": None})
            n = store.mark_interrupted()
            self.assertEqual(n, 1)
            old = webui.STORE
            webui.STORE = store
            try:
                _, listing = self._get_json("/api/jobs?limit=5")
                row = next(j for j in listing["jobs"] if j["job_id"] == "hist1")
                self.assertEqual(row["status"], "interrupted")
            finally:
                webui.STORE = old

    def test_t02_cancel_running_job(self):
        gate = threading.Event()
        fake = _GatedPopen(gate)
        with mock.patch("subprocess.Popen", side_effect=fake), \
             mock.patch.object(webui_jobs, "_kill_tree"):
            status, data = self._post_json("/api/jobs", {
                "mode": "single", "url": URL,
                "want_transcript": True, "want_frames": False})
            self.assertEqual(status, 200)
            job_id = data["job_id"]
            deadline = time.time() + 15
            info = None
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] == "running":
                    break
                time.sleep(0.05)
            self.assertEqual(info["status"], "running")
            status, data = self._post_json(f"/api/jobs/{job_id}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "cancelling")
            gate.set()   # 放子进程退出，run_job 察觉取消并收尾
            deadline = time.time() + 15
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] not in ("cancelling", "running"):
                    break
                time.sleep(0.05)
            self.assertEqual(info["status"], "cancelled")

    def test_t02_cancel_unknown_and_finished(self):
        status, data = self._post_json("/api/jobs/nope/cancel", {})
        self.assertEqual(status, 404)
        # 已结束任务 → 409
        out = webui.RESULT_PREFIX + json.dumps(
            self._success_result(), ensure_ascii=False)
        with mock.patch("subprocess.Popen",
                        side_effect=_FakePopen("[watch] ✅ 完成\n" + out + "\n")):
            _, data = self._post_json("/api/jobs", {
                "mode": "single", "url": URL,
                "want_transcript": True, "want_frames": False})
            job_id = data["job_id"]
            deadline = time.time() + 15
            while time.time() < deadline:
                _, info = self._get_json(f"/api/jobs/{job_id}")
                if info["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
            self.assertEqual(info["status"], "done")
            status, data = self._post_json(f"/api/jobs/{job_id}/cancel", {})
            self.assertEqual(status, 409)

    def test_t03_frames_only_job_reuses_cached_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"fake")
            store = webui_jobs.JobStore(Path(tmp) / "t.db")
            store.media_set(webui_jobs.media_key(URL), str(Path(tmp) / "old_run"),
                            str(video))
            old = webui.STORE
            webui.STORE = store
            captured = []

            def factory(cmd, **kwargs):
                captured.append(cmd)
                out = webui.RESULT_PREFIX + json.dumps(
                    {"ok": True, "run_dir": str(Path(tmp) / "new_run")},
                    ensure_ascii=False)
                return _FakePopen("[watch] ✅ 完成\n" + out + "\n")(cmd)

            try:
                with mock.patch("subprocess.Popen", side_effect=factory):
                    job = webui.Job("single", [URL], False, True)   # 纯抽帧
                    webui.run_job(job)
            finally:
                webui.STORE = old
            self.assertIn(str(video), captured[0])   # 用缓存媒体而非 URL
            self.assertNotIn(URL, captured[0])
            self.assertEqual(job.results[0].get("reused_from"),
                             str(Path(tmp) / "old_run"))

    def test_u08_dedup_restore_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            run_dir = root / "r1"
            frames = run_dir / "frames"
            frames.mkdir(parents=True)
            (frames / "b.jpg").write_bytes(b"\xff\xd8")
            (frames / "frames.json").write_text(json.dumps(
                [{"file": "b.jpg", "t": 1.0, "dropped": True}]), encoding="utf-8")
            with mock.patch.object(webui, "RUNS_ROOT", root):
                status, data = self._post_json("/api/dedup_restore",
                                               {"run_dir": str(run_dir), "file": "b.jpg"})
                self.assertEqual(status, 200)
                self.assertTrue(data["ok"])
                self.assertEqual(data["kept_count"], 1)
                # 越界 → 403；非法文件名 → 400
                status, _ = self._post_json("/api/dedup_restore",
                                            {"run_dir": str(root / ".." / "x"),
                                             "file": "b.jpg"})
                self.assertEqual(status, 403)
                status, _ = self._post_json("/api/dedup_restore",
                                            {"run_dir": str(run_dir),
                                             "file": "../evil.jpg"})
                self.assertEqual(status, 400)

    def test_static_assets_served_with_boundary(self):
        # T06：/static/ 路由正常返回 webui/ 内资源；路径穿越与白名单外后缀拒绝
        status, body = self._get("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--accent", body)
        status, body = self._get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"requiredChannels", body)
        status, _ = self._get("/static/" + urllib.parse.quote("../scripts/webui.py"))
        self.assertEqual(status, 404)
        status, _ = self._get("/static/" + urllib.parse.quote("..\\..\\scripts\\watch.py"))
        self.assertEqual(status, 404)
        status, _ = self._get("/static/nope.css")
        self.assertEqual(status, 404)
        status, _ = self._get("/static/" + urllib.parse.quote("index.html"))
        # .html 不在静态白名单（页面只从 / 出）
        self.assertEqual(status, 404)

    def test_unknown_job_returns_404(self):
        status, data = self._get_json("/api/jobs/nope")
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])

    # ---- POST /api/export_pdf ----

    def test_export_pdf_traversal_returns_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            root.mkdir()
            outside = (Path(tmp) / "elsewhere").resolve()
            outside.mkdir()
            with mock.patch.object(webui, "RUNS_ROOT", root):
                status, data = self._post_json("/api/export_pdf",
                                               {"run_dir": str(outside)})
            self.assertEqual(status, 403)
            self.assertFalse(data["ok"])

    def test_export_pdf_success_with_mocked_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            run_dir = root / "视频_20260101"
            (run_dir / "frames").mkdir(parents=True)
            payload = {"ok": True, "pdf": str(run_dir / "关键帧.pdf"),
                       "pages": 1, "frames_included": 3, "frames_skipped": 0}
            fake = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="[frames-pdf] 已生成\n" + webui.RESULT_PREFIX
                       + json.dumps(payload, ensure_ascii=False) + "\n",
                stderr="")
            with mock.patch.object(webui, "RUNS_ROOT", root), \
                 mock.patch("subprocess.run", return_value=fake) as mrun:
                status, data = self._post_json("/api/export_pdf",
                                               {"run_dir": str(run_dir)})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["pdf"].endswith("关键帧.pdf"))
            self.assertEqual(data["frames_included"], 3)
            # 子进程以 list 形式调用 make_frames_pdf.py --run-dir
            cmd = mrun.call_args[0][0]
            self.assertIsInstance(cmd, list)
            self.assertTrue(any("make_frames_pdf.py" in str(c) for c in cmd))
            self.assertIn("--run-dir", cmd)

    def test_export_pdf_failure_propagates_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            run_dir = root / "空目录_20260101"
            run_dir.mkdir(parents=True)
            payload = {"ok": False, "error": "找不到 frames.json"}
            fake = subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout=webui.RESULT_PREFIX
                       + json.dumps(payload, ensure_ascii=False) + "\n",
                stderr="[frames-pdf][ERROR] 找不到 frames.json")
            with mock.patch.object(webui, "RUNS_ROOT", root), \
                 mock.patch("subprocess.run", return_value=fake):
                status, data = self._post_json("/api/export_pdf",
                                               {"run_dir": str(run_dir)})
            self.assertEqual(status, 500)
            self.assertFalse(data["ok"])
            self.assertIn("frames.json", data["error"])

    def test_export_pdf_missing_run_dir_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "runs").resolve()
            root.mkdir()
            with mock.patch.object(webui, "RUNS_ROOT", root):
                status, data = self._post_json("/api/export_pdf",
                                               {"run_dir": str(root / "不存在")})
            self.assertEqual(status, 404)
            self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
