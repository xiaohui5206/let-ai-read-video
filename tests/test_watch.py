import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import watch  # noqa: E402


class WatchHelpersTests(unittest.TestCase):
    def test_display_arg_redacts_url_credentials_and_query(self):
        value = "https://user:secret@example.com:8443/video?id=1&token=abc#part"
        self.assertEqual(
            watch._display_arg(value),
            "https://example.com:8443/video",
        )

    def test_display_arg_preserves_local_path(self):
        value = r"C:\Videos\meeting.mp4"
        self.assertEqual(watch._display_arg(value), value)

    def test_atomic_json_write_replaces_complete_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text('{"old": true}', encoding="utf-8")

            watch._write_json_atomic(path, {"schema_version": 2, "ok": True})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema_version": 2, "ok": True},
            )
            self.assertFalse(path.with_name("manifest.json.tmp").exists())


class FpsPassthroughTests(unittest.TestCase):
    """watch.py --fps 透传 frames.py（process_one 的 fr_args），默认 None 不出现。"""

    def _run_process_one(self, fps):
        captured = {}

        def fake_step(script, cli_args, label, fatal=True):
            if script == "frames.py":
                captured["fr_args"] = list(cli_args)
                return {"frames_json": "f.json", "count": 1, "pass_id": "base",
                        "window": None}
            return {}

        args = argparse.Namespace(
            input="v.mp4", item=None, start=None, end=None, max_frames=None,
            fps=fps, budget=None, width=512, mode="auto", engine="faster-whisper",
            model="small", language="auto", device="auto", no_frames=False,
            no_transcribe=True, force_whisper=False, refine_plan=None,
            refine_pass_id="r1", max_extra_frames=60, no_review=True,
            step_timeout=0, out_dir=None)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {"step": fake_step, "run_dir": Path(tmp), "kind": "file",
                   "title": "t", "duration": 10.0, "has_video": True,
                   "has_audio": True, "video_path": "v.mp4", "audio_path": "v.mp4",
                   "captions": [], "timeline": {}, "start_s": None, "end_s": None}
            with contextlib.redirect_stdout(io.StringIO()):
                watch.process_one(args, ctx)
        return captured.get("fr_args", [])

    def test_fps_passthrough(self):
        fr_args = self._run_process_one(0.2)
        self.assertEqual(fr_args[fr_args.index("--fps") + 1], "0.2")

    def test_fps_default_absent(self):
        self.assertNotIn("--fps", self._run_process_one(None))


class RecheckMediaTests(unittest.TestCase):
    """C01：下载后对实际文件 ffprobe 复检，覆盖 probe 元数据。"""

    def _fake_ffprobe_json(self, streams, duration="12.5"):
        return json.dumps({"format": {"duration": duration},
                           "streams": streams})

    def _run_recheck(self, proc=None, tool="ffprobe"):
        with mock.patch.object(watch, "find_tool", return_value=tool), \
             mock.patch("subprocess.run", return_value=proc):
            return watch.recheck_media("real.mp4")

    def test_c01_av_file_detects_both_tracks(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._fake_ffprobe_json([
                {"codec_type": "video", "width": 640, "disposition": {"attached_pic": 0}},
                {"codec_type": "audio"},
            ]), stderr="")
        out = self._run_recheck(proc)
        self.assertTrue(out["has_video"])
        self.assertTrue(out["has_audio"])
        self.assertEqual(out["duration"], 12.5)

    def test_c01_no_audio_track(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._fake_ffprobe_json([
                {"codec_type": "video", "width": 640, "disposition": {"attached_pic": 0}},
            ]), stderr="")
        out = self._run_recheck(proc)
        self.assertTrue(out["has_video"])
        self.assertFalse(out["has_audio"])   # 无音轨明确标注，照常抽帧

    def test_c01_attached_pic_not_counted_as_video(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._fake_ffprobe_json([
                {"codec_type": "video", "width": 640, "disposition": {"attached_pic": 1}},
                {"codec_type": "audio"},
            ]), stderr="")
        out = self._run_recheck(proc)
        self.assertFalse(out["has_video"])
        self.assertTrue(out["has_audio"])

    def test_c01_failure_returns_empty(self):
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
        self.assertEqual(self._run_recheck(proc), {})
        self.assertEqual(self._run_recheck(tool=None), {})
        self.assertEqual(watch.recheck_media(None), {})


if __name__ == "__main__":
    unittest.main()
