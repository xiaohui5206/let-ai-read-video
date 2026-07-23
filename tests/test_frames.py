from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FRAMES_PATH = ROOT / "scripts" / "frames.py"
SPEC = importlib.util.spec_from_file_location("video_watch_frames", FRAMES_PATH)
assert SPEC is not None and SPEC.loader is not None
frames = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = frames
SPEC.loader.exec_module(frames)


def result_from_stdout(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.startswith("RESULT_JSON: ")]
    if not lines:
        raise AssertionError(f"missing RESULT_JSON in output: {text}")
    return json.loads(lines[-1][len("RESULT_JSON: "):])


class PointPlanningTests(unittest.TestCase):
    def test_auto_reserves_uniform_backbone_and_covers_tail(self) -> None:
        scenes = [float(i) for i in range(1, 21)]
        points = frames.plan_points("auto", scenes, 0.0, 100.0, 10)

        self.assertEqual(len(points), 10)
        uniform = [t for t, source in points if source == "uniform"]
        self.assertGreaterEqual(len(uniform), 5)
        self.assertGreaterEqual(max(uniform), 90.0)

        times = [0.0] + [t for t, _ in points] + [100.0]
        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 20.0)

    def test_auto_fills_budget_at_two_fps_limit(self) -> None:
        points = frames.plan_points("auto", [], 0.0, 1.0, 2)

        self.assertEqual(len(points), 2)
        self.assertGreaterEqual(points[1][0] - points[0][0], frames.MIN_GAP)

    def test_max_spacing_tie_prefers_later_candidate(self) -> None:
        selected = frames.select_max_spacing(
            [1.0, 2.0, 99.0],
            occupied=[25.0, 50.0, 75.0],
            n=1,
        )
        self.assertEqual(selected, [99.0])


class TimesSpecTests(unittest.TestCase):
    def test_loads_direct_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "times.json"
            path.write_text(
                json.dumps([1.0, {"t": 2.5, "reason": "cut", "source": "review"}]),
                encoding="utf-8",
            )

            points, pass_id = frames.load_times_spec(path)

        self.assertIsNone(pass_id)
        self.assertEqual([point["t"] for point in points], [1.0, 2.5])
        self.assertEqual(points[0]["source"], "targeted")
        self.assertEqual(points[1]["reason"], "cut")

    def test_loads_envelope_pass_id_and_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "times.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "pass_id": "r2",
                    "times": [{"t": 3.25, "width": 1024}],
                }),
                encoding="utf-8",
            )

            points, pass_id = frames.load_times_spec(path)

        self.assertEqual(pass_id, "r2")
        self.assertEqual(points[0]["width"], 1024)

    def test_quarter_second_dedupe_for_targeted_mode(self) -> None:
        points = [
            {"t": 1.0},
            {"t": 1.25},
            {"t": 1.49},
        ]

        kept, skipped = frames.dedupe_target_points(
            points,
            gap=frames.TARGETED_MIN_GAP,
        )

        self.assertEqual([point["t"] for point in kept], [1.0, 1.25])
        self.assertEqual(skipped, 1)


class ExtractionAndIndexTests(unittest.TestCase):
    def test_loading_existing_index_preserves_four_fps_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frames.json"
            path.write_text(
                json.dumps([
                    {"file": "0000_t000001.0.jpg", "t": 1.0, "source": "targeted"},
                    {"file": "0001_t000001.2.jpg", "t": 1.233, "source": "targeted"},
                    {"file": "0002_t000001.5.jpg", "t": 1.5, "source": "targeted"},
                ]),
                encoding="utf-8",
            )

            entries = frames.load_existing_entries(path)

        self.assertEqual([entry["t"] for entry in entries], [1.0, 1.233, 1.5])

    def test_extract_one_reports_fallback_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "frame.jpg"
            calls = []

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                if len(calls) == 2:
                    Path(cmd[-1]).write_bytes(b"jpeg")
                    return SimpleNamespace(returncode=0, stderr="", stdout="")
                return SimpleNamespace(returncode=1, stderr="seek failed", stdout="")

            with mock.patch.object(frames.subprocess, "run", side_effect=fake_run):
                ok, error, actual_t = frames.extract_one(
                    "ffmpeg", "video.mp4", 10.0, 512, out_path
                )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertAlmostEqual(actual_t, 9.4)

    def test_extract_one_reports_decoded_frame_pts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "frame.jpg"

            def fake_run(cmd, **_kwargs):
                out_path.write_bytes(b"jpeg")
                return SimpleNamespace(
                    returncode=0,
                    stderr=(
                        "[Parsed_showinfo_0] n: 0 pts: 128 "
                        "pts_time:0.00833333 duration: 512"
                    ),
                    stdout="",
                )

            with mock.patch.object(frames.subprocess, "run", side_effect=fake_run):
                ok, error, actual_t = frames.extract_one(
                    "ffmpeg", "video.mp4", 3.625, 512, out_path
                )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertAlmostEqual(actual_t, 3.63333333)

    def test_atomic_json_failure_preserves_old_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frames.json"
            path.write_text('{"old": true}', encoding="utf-8")

            with mock.patch.object(frames.os, "replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    frames.write_json_atomic(path, [{"t": 1.0}])

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(list(path.parent.glob(".frames.json.*.tmp")), [])


class MainIntegrationTests(unittest.TestCase):
    def run_main(
        self,
        argv: list[str],
        duration: float,
        actual_offset=0.0,
    ) -> tuple[dict, str]:
        def fake_extract(_ffmpeg, _video, requested_t, _width, out_path):
            Path(out_path).write_bytes(b"jpeg")
            if callable(actual_offset):
                actual_t = actual_offset(requested_t)
            else:
                actual_t = requested_t + actual_offset
            return True, "", actual_t

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["frames.py", *argv]),
            mock.patch.object(frames, "find_tool", return_value="tool"),
            mock.patch.object(frames, "probe_duration", return_value=duration),
            mock.patch.object(frames, "detect_scene_times", return_value=[]),
            mock.patch.object(frames, "extract_one", side_effect=fake_extract),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            frames.main()
        return result_from_stdout(stdout.getvalue()), stderr.getvalue()

    def test_targeted_mode_accepts_explicit_four_fps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            times = root / "times.json"
            times.write_text(
                json.dumps({
                    "version": 1,
                    "pass_id": "r1",
                    "times": [1.0, 1.25, 1.5, 1.75],
                }),
                encoding="utf-8",
            )

            result, _ = self.run_main([
                "--video", str(video),
                "--out-dir", str(root / "run"),
                "--times-json", str(times),
                "--fps", "4",
            ], duration=10.0)

            index = json.loads(Path(result["frames_json"]).read_text(encoding="utf-8"))

        self.assertEqual(result["sampling_mode"], "targeted")
        self.assertEqual(result["fps_cap"], 4.0)
        self.assertEqual(result["pass_id"], "r1")
        self.assertEqual(result["count"], 4)
        self.assertEqual([entry["t"] for entry in index], [1.0, 1.25, 1.5, 1.75])

    def test_targeted_mode_keeps_unique_frames_after_timebase_quantisation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            times = root / "times.json"
            times.write_text(
                json.dumps({
                    "version": 1,
                    "times": [1.0, 1.25, 1.5],
                }),
                encoding="utf-8",
            )
            actual = {1.0: 1.0, 1.25: 1.233, 1.5: 1.5}

            result, _ = self.run_main([
                "--video", str(video),
                "--out-dir", str(root / "run"),
                "--times-json", str(times),
                "--fps", "4",
            ], duration=10.0, actual_offset=actual.__getitem__)

            index = json.loads(Path(result["frames_json"]).read_text(encoding="utf-8"))

        self.assertEqual(result["count"], 3)
        self.assertEqual([entry["actual_t"] for entry in index], [1.0, 1.233, 1.5])

    def test_base_mode_still_caps_explicit_fps_at_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"video")

            result, _ = self.run_main([
                "--video", str(video),
                "--out-dir", str(root / "run"),
                "--mode", "uniform",
                "--budget", "100",
                "--fps", "4",
            ], duration=1.0)

        self.assertEqual(result["sampling_mode"], "base")
        self.assertEqual(result["fps_cap"], 2.0)
        self.assertEqual(result["count"], 2)

    def test_append_preserves_old_frame_and_records_actual_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            run_dir = root / "run"
            frames_dir = run_dir / "frames"
            frames_dir.mkdir(parents=True)
            old_file = frames_dir / "0000_t000010.0.jpg"
            old_file.write_bytes(b"old")
            (frames_dir / "frames.json").write_text(
                json.dumps([{"file": old_file.name, "t": 10.0, "source": "uniform"}]),
                encoding="utf-8",
            )
            times = root / "times.json"
            times.write_text(
                json.dumps({
                    "version": 1,
                    "pass_id": "r2",
                    "times": [
                        {"t": 10.1, "reason": "duplicate"},
                        {"t": 20.0, "reason": "transition", "source": "review"},
                    ],
                }),
                encoding="utf-8",
            )

            result, _ = self.run_main([
                "--video", str(video),
                "--out-dir", str(run_dir),
                "--times-json", str(times),
                "--append",
            ], duration=30.0, actual_offset=-0.6)

            index = json.loads(Path(result["frames_json"]).read_text(encoding="utf-8"))
            old_file_still_exists = old_file.exists()

        self.assertTrue(old_file_still_exists)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["count"], 2)
        self.assertEqual(index[1]["requested_t"], 20.0)
        self.assertEqual(index[1]["actual_t"], 19.4)
        self.assertEqual(index[1]["t"], 19.4)
        self.assertEqual(index[1]["pass_id"], "r2")
        self.assertIn("000019.4", index[1]["file"])


if __name__ == "__main__":
    unittest.main()
