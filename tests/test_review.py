import sys
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import review  # noqa: E402
import refine  # noqa: E402


class ReviewPacketTests(unittest.TestCase):
    def test_prepare_units_marks_visual_cue_without_close_frame(self):
        transcript = [{
            "start": 3.0,
            "end": 5.0,
            "text": "请看这里的曲线变化",
        }]
        frames = [{"file": "f.jpg", "path": "f.jpg", "t": 9.0,
                   "source": "uniform", "pass_id": "base"}]

        units = review.prepare_units(
            transcript, frames, window=10.0, duration=10.0, attention_gap=6.0
        )

        self.assertEqual(len(units), 1)
        self.assertTrue(units[0]["signals"]["needs_attention"])
        self.assertIn("看这里", units[0]["signals"]["visual_cues"])

    def test_prepare_units_respects_absolute_focus_range(self):
        transcript = [{"start": 750.0, "end": 755.0, "text": "focused"}]
        frames = [{
            "t": 752.0,
            "file": "focused.jpg",
            "path": "focused.jpg",
            "source": "uniform",
            "pass_id": "base",
        }]

        units = review.prepare_units(
            transcript,
            frames,
            window=10.0,
            duration=3600.0,
            attention_gap=5.0,
            range_start=750.0,
            range_end=780.0,
        )

        self.assertEqual(len(units), 3)
        self.assertEqual((units[0]["start"], units[0]["end"]), (750.0, 760.0))
        self.assertEqual((units[-1]["start"], units[-1]["end"]), (770.0, 780.0))

    def test_prepare_units_supports_visual_only_review(self):
        frames = [{
            "t": 4.0,
            "file": "visual.jpg",
            "path": "visual.jpg",
            "source": "scene",
            "pass_id": "base",
        }]

        units = review.prepare_units(
            [],
            frames,
            window=10.0,
            duration=20.0,
            attention_gap=5.0,
        )

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]["transcript"], "")
        self.assertEqual(units[0]["signals"]["frame_count"], 1)
        self.assertTrue(units[1]["signals"]["needs_attention"])

    def test_build_plan_only_refines_supported_labels(self):
        packet = {
            "duration": 30.0,
            "source": {"frames_json": "frames.json"},
            "units": [
                {
                    "id": "w0000", "start": 0.0, "end": 10.0,
                    "signals": {"needs_attention": False},
                    "assessment": {
                        "relation": "complements", "confidence": 0.9,
                        "notes": "", "refine": False,
                    },
                },
                {
                    "id": "w0001", "start": 10.0, "end": 20.0,
                    "signals": {"needs_attention": False},
                    "assessment": {
                        "relation": "insufficient", "confidence": 0.8,
                        "notes": "missing demo step", "refine": None,
                    },
                },
            ],
        }

        plan = review.build_plan(
            packet, min_confidence=0.6, padding=1.0, fps=2.0,
            include_heuristics=False,
        )

        self.assertEqual(len(plan["intervals"]), 1)
        self.assertEqual(plan["intervals"][0]["start"], 9.0)
        self.assertEqual(plan["intervals"][0]["end"], 21.0)
        self.assertEqual(plan["intervals"][0]["reason"], "missing demo step")
        compiled, warnings = refine.compile_plan(plan, max_extra=60, width=512)
        self.assertTrue(compiled)
        self.assertEqual(warnings, [])

    def test_merge_intervals_keeps_disjoint_windows(self):
        merged = review.merge_intervals([
            {"start": 0.0, "end": 5.0, "reason": "a", "fps": 1.0},
            {"start": 4.0, "end": 8.0, "reason": "b", "fps": 2.0},
            {"start": 12.0, "end": 15.0, "reason": "c", "fps": 1.0},
        ])

        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (0.0, 8.0))
        self.assertEqual(merged[0]["fps"], 2.0)

    def test_normalize_transcript_tolerates_tiny_negative_start(self):
        # B 站缓存偏移 -0.023s 产生的微负 start 应归一化为 0.0 而非报错
        items = review.normalize_transcript([
            {"start": -0.023, "end": 1.5, "text": "first"},
        ])

        self.assertEqual(items[0]["start"], 0.0)
        self.assertEqual(items[0]["end"], 1.5)
        self.assertEqual(items[0]["text"], "first")

    def test_normalize_transcript_still_rejects_illegal_ranges(self):
        quiet_out, quiet_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(quiet_out), contextlib.redirect_stderr(quiet_err):
            # 超出 -1.0s 容差的负 start 仍报错
            with self.assertRaises(SystemExit):
                review.normalize_transcript([
                    {"start": -2.0, "end": 1.5, "text": "bad"},
                ])
            # end<start 的非法检测保留
            with self.assertRaises(SystemExit):
                review.normalize_transcript([
                    {"start": 2.0, "end": 1.0, "text": "bad"},
                ])

    def test_refresh_with_broken_manifest_leaves_review_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames_path = root / "frames.json"
            frames_path.write_text(
                json.dumps([{"file": "a.jpg", "t": 1.0, "pass_id": "base"}]),
                encoding="utf-8",
            )
            packet = {
                "version": 1,
                "source": {
                    "transcript_json": None,
                    "frames_json": str(frames_path),
                },
                "duration": 20.0,
                "range": {"start": 0.0, "end": 20.0},
                "window_seconds": 10.0,
                "attention_gap_seconds": 5.0,
                "units": [],
            }
            review_path = root / "review.json"
            review_path.write_text(json.dumps(packet), encoding="utf-8")
            (root / "manifest.json").write_text("{broken json", encoding="utf-8")
            original = review_path.read_text(encoding="utf-8")

            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as ctx,
            ):
                review.main(["refresh", "--review", str(review_path)])

            after = review_path.read_text(encoding="utf-8")

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(after, original)

    def test_refresh_resets_only_windows_with_changed_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript_path = root / "transcript.json"
            transcript_path.write_text(
                json.dumps([
                    {"start": 1.0, "end": 2.0, "text": "first"},
                    {"start": 11.0, "end": 12.0, "text": "second"},
                ]),
                encoding="utf-8",
            )
            frames_path = root / "frames.json"
            frames_path.write_text(
                json.dumps([
                    {"file": "a.jpg", "t": 1.0, "pass_id": "base"},
                    {"file": "b.jpg", "t": 11.0, "pass_id": "base"},
                ]),
                encoding="utf-8",
            )
            units = review.prepare_units(
                review.normalize_transcript(json.loads(transcript_path.read_text())),
                review.normalize_frames(
                    json.loads(frames_path.read_text()),
                    root,
                ),
                window=10.0,
                duration=20.0,
                attention_gap=5.0,
            )
            for unit in units:
                unit["assessment"] = {
                    "relation": "supports",
                    "confidence": 0.9,
                    "notes": "checked",
                    "refine": False,
                }
            packet = {
                "version": 1,
                "source": {
                    "transcript_json": str(transcript_path),
                    "frames_json": str(frames_path),
                },
                "duration": 20.0,
                "range": {"start": 0.0, "end": 20.0},
                "window_seconds": 10.0,
                "attention_gap_seconds": 5.0,
                "units": units,
            }
            review_path = root / "review.json"
            review_path.write_text(json.dumps(packet), encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"review": {"status": "stale_after_refinement"}}),
                encoding="utf-8",
            )
            frames_path.write_text(
                json.dumps([
                    {"file": "a.jpg", "t": 1.0, "pass_id": "base"},
                    {"file": "new.jpg", "t": 2.0, "pass_id": "r1"},
                    {"file": "b.jpg", "t": 11.0, "pass_id": "base"},
                ]),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                code = review.main(["refresh", "--review", str(review_path)])

            refreshed = json.loads(review_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertIsNone(refreshed["units"][0]["assessment"]["relation"])
        self.assertEqual(
            refreshed["units"][1]["assessment"]["relation"],
            "supports",
        )
        self.assertEqual(
            manifest["review"]["status"],
            "pending_reassessment",
        )


if __name__ == "__main__":
    unittest.main()
