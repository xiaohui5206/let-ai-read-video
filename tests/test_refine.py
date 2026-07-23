from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REFINE_PATH = ROOT / "scripts" / "refine.py"
SPEC = importlib.util.spec_from_file_location("video_watch_refine", REFINE_PATH)
assert SPEC is not None and SPEC.loader is not None
refine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refine
SPEC.loader.exec_module(refine)


class CompilePlanTests(unittest.TestCase):
    def test_defaults_dedupe_and_explicit_metadata_wins(self) -> None:
        plan = {
            "version": 1,
            "intervals": [
                {
                    "start": 0,
                    "end": 1,
                    "reason": "motion",
                    "width": 1024,
                }
            ],
            "times": [
                {"t": 0.25, "reason": "named point", "source": "review"},
                2,
                2.0,
            ],
        }
        times, warnings = refine.compile_plan(plan, max_extra=60, width=512)
        self.assertEqual([item["t"] for item in times], [0.25, 0.75, 2.0])
        self.assertEqual(times[0]["reason"], "named point")
        self.assertEqual(times[0]["source"], "review")
        self.assertIn("normalised", warnings[0])

    def test_interval_fps_must_not_exceed_four(self) -> None:
        plan = {
            "version": 1,
            "intervals": [
                {"start": 0, "end": 2, "reason": "dense", "fps": 4.01}
            ],
        }
        with self.assertRaisesRegex(refine.RefineError, "local limit"):
            refine.compile_plan(plan)

    def test_global_budget_preserves_explicit_points_first(self) -> None:
        plan = {
            "version": 1,
            "intervals": [
                {"start": 0, "end": 20, "reason": "wide scan", "fps": 4}
            ],
            "times": [
                {"t": 50, "reason": "explicit A"},
                {"t": 60, "reason": "explicit B"},
            ],
        }
        times, warnings = refine.compile_plan(plan, max_extra=5)
        self.assertEqual(len(times), 5)
        self.assertIn(50.0, [item["t"] for item in times])
        self.assertIn(60.0, [item["t"] for item in times])
        self.assertTrue(any("reduced" in warning for warning in warnings))

    def test_explicit_only_plan_is_evenly_capped(self) -> None:
        plan = {"version": 1, "times": list(range(10))}
        times, _ = refine.compile_plan(plan, max_extra=3)
        self.assertEqual([item["t"] for item in times], [0.0, 4.0, 9.0])

    def test_strict_schema_rejects_unknown_fields_and_booleans(self) -> None:
        cases = [
            {"version": 1, "times": [{"t": 1, "reason": "x", "extra": 2}]},
            {"version": 1, "times": [True]},
            {
                "version": 1,
                "intervals": [{"start": 0, "end": 1, "reason": "x", "fps": True}],
            },
            {"version": 2, "times": [1]},
        ]
        for plan in cases:
            with self.subTest(plan=plan):
                with self.assertRaises(refine.RefineError):
                    refine.compile_plan(plan)

    def test_empty_plan_and_reversed_interval_fail(self) -> None:
        with self.assertRaisesRegex(refine.RefineError, "at least one"):
            refine.compile_plan({"version": 1})
        with self.assertRaisesRegex(refine.RefineError, "greater than start"):
            refine.compile_plan(
                {
                    "version": 1,
                    "intervals": [
                        {"start": 3, "end": 2, "reason": "invalid"}
                    ],
                }
            )


class FileAndCliTests(unittest.TestCase):
    def _write_fake_frames(self, directory: Path, *, fail: bool = False) -> Path:
        script = directory / "fake frames.py"
        if fail:
            source = (
                "import json, sys\n"
                "print('fake diagnostic')\n"
                "print('RESULT_JSON: ' + json.dumps("
                "{'ok': False, 'error': 'synthetic failure'}))\n"
                "raise SystemExit(7)\n"
            )
        else:
            source = (
                "import argparse, json\n"
                "from pathlib import Path\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--video'); p.add_argument('--out-dir')\n"
                "p.add_argument('--width'); p.add_argument('--fps')\n"
                "p.add_argument('--times-json')\n"
                "p.add_argument('--append', action='store_true')\n"
                "p.add_argument('--pass-id')\n"
                "a = p.parse_args()\n"
                "doc = json.loads(Path(a.times_json).read_text(encoding='utf-8'))\n"
                "fd = Path(a.out_dir) / 'frames'; fd.mkdir(parents=True, exist_ok=True)\n"
                "fj = fd / 'frames.json'; fj.write_text('[]\\n', encoding='utf-8')\n"
                "n = len(doc['times'])\n"
                "print('fake frames completed')\n"
                "print('RESULT_JSON: ' + json.dumps({'ok': True, 'count': 9, "
                "'added': n, 'frames_json': str(fj), 'fps': a.fps}))\n"
            )
        script.write_text(source, encoding="utf-8")
        return script

    def _last_result(self, stdout: str) -> dict:
        lines = [
            line
            for line in stdout.splitlines()
            if line.startswith(refine.RESULT_PREFIX)
        ]
        self.assertTrue(lines)
        return json.loads(lines[-1][len(refine.RESULT_PREFIX) :])

    def test_cli_writes_times_calls_frames_and_updates_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="refine test ") as temp:
            root = Path(temp)
            video = root / "input video.mp4"
            video.write_bytes(b"placeholder")
            plan = root / "plan file.json"
            plan.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "intervals": [
                            {"start": 0, "end": 1, "reason": "inspect"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = root / "run output"
            out_dir.mkdir()
            manifest = out_dir / "manifest.json"
            manifest.write_text(
                json.dumps({"title": "sample", "frames": {"count": 7}}),
                encoding="utf-8",
            )
            fake_frames = self._write_fake_frames(root)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = refine.main(
                    [
                        "--video",
                        str(video),
                        "--out-dir",
                        str(out_dir),
                        "--plan",
                        str(plan),
                        "--pass-id",
                        "pass-2",
                    ],
                    frames_script=fake_frames,
                )

            self.assertEqual(code, 0, stderr.getvalue())
            result = self._last_result(stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["added_count"], 2)
            times_path = out_dir / "refine_times_pass-2.json"
            document = json.loads(times_path.read_text(encoding="utf-8"))
            self.assertEqual(document["pass_id"], "pass-2")
            self.assertEqual(document["max_fps"], 4.0)
            self.assertEqual(len(document["times"]), 2)
            self.assertEqual(result["frames_result"]["fps"], "4")

            updated = json.loads(manifest.read_text(encoding="utf-8"))
            passes = updated["frames"]["passes"]
            self.assertEqual(len(passes), 1)
            self.assertEqual(passes[0]["pass_id"], "pass-2")
            self.assertEqual(passes[0]["added_count"], 2)
            self.assertEqual(passes[0]["total_count"], 9)
            self.assertEqual(passes[0]["plan"], str(plan.resolve()))
            self.assertEqual(updated["frames"]["count"], 9)
            self.assertEqual(updated["frames"]["json"], passes[0]["frames_json"])

    def test_child_failure_is_propagated_in_result_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="refine failure ") as temp:
            root = Path(temp)
            video = root / "input.mp4"
            video.write_bytes(b"x")
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"version": 1, "times": [1]}), encoding="utf-8"
            )
            out_dir = root / "out"
            fake_frames = self._write_fake_frames(root, fail=True)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = refine.main(
                    [
                        "--video",
                        str(video),
                        "--out-dir",
                        str(out_dir),
                        "--plan",
                        str(plan),
                        "--pass-id",
                        "retry",
                    ],
                    frames_script=fake_frames,
                )
            result = self._last_result(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["returncode"], 7)
            self.assertEqual(result["frames_result"]["error"], "synthetic failure")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / "plan.json"
            plan.write_text(
                '{"version": 1, "version": 1, "times": [1]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(refine.RefineError, "duplicate JSON key"):
                refine.load_json(plan, "plan")

    def test_infer_width_reuses_existing_run_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(
                json.dumps({"params": {"width": 1024}}),
                encoding="utf-8",
            )

            width = refine.infer_existing_width(root)

        self.assertEqual(width, 1024)

    def test_refinement_allowance_enforces_pass_and_total_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(
                json.dumps({
                    "frames": {
                        "base_count": 10,
                        "count": 15,
                        "passes": [
                            {"pass_id": "base", "count": 10},
                            {"pass_id": "r1", "count": 5, "plan": "r1.json"},
                        ],
                    }
                }),
                encoding="utf-8",
            )

            remaining = refine.refinement_allowance(
                root,
                pass_id="r2",
                max_passes=2,
                max_total_extra=6,
            )

            self.assertEqual(remaining, 1)
            with self.assertRaisesRegex(refine.RefineError, "pass limit"):
                refine.refinement_allowance(
                    root,
                    pass_id="r3",
                    max_passes=1,
                    max_total_extra=120,
                )

    def test_manifest_upsert_replaces_same_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "frames": {
                            "passes": [
                                {
                                    "pass_id": "same",
                                    "added_count": 1,
                                    "custom": "old",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            plan = root / "plan.json"
            plan.write_text("{}", encoding="utf-8")
            updated, _, record = refine.update_manifest(
                out_dir=root,
                pass_id="same",
                plan_path=plan,
                requested_count=3,
                frames_result={
                    "ok": True,
                    "added": 3,
                    "count": 12,
                    "frames_json": "frames/frames.json",
                },
            )
            self.assertTrue(updated)
            self.assertEqual(record["added_count"], 3)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["frames"]["passes"]), 1)
            self.assertNotIn("custom", saved["frames"]["passes"][0])
            self.assertEqual(saved["frames"]["count"], 12)
            self.assertTrue(saved["frames"]["dir"].endswith("frames"))

    def test_unsafe_pass_id_is_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "input.mp4"
            video.write_bytes(b"x")
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"version": 1, "times": [1]}), encoding="utf-8"
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = refine.main(
                    [
                        "--video",
                        str(video),
                        "--out-dir",
                        str(root / "out"),
                        "--plan",
                        str(plan),
                        "--pass-id",
                        "../escape",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertFalse(self._last_result(stdout.getvalue())["ok"])
            self.assertFalse((root / "escape.json").exists())


class MissingManifestTests(unittest.TestCase):
    @staticmethod
    def _accumulating_frames(**kwargs: object) -> dict:
        """模拟 frames.py 追加模式：累积写入索引并返回累计计数。"""
        out_dir = Path(str(kwargs["out_dir"]))
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames_json = frames_dir / "frames.json"
        if frames_json.is_file():
            entries = json.loads(frames_json.read_text(encoding="utf-8"))
        else:
            entries = []
        document = json.loads(
            Path(str(kwargs["times_json"])).read_text(encoding="utf-8")
        )
        pass_id = str(kwargs["pass_id"])
        for item in document["times"]:
            entries.append(
                {
                    "file": f"{pass_id}_{len(entries):04d}.jpg",
                    "t": item["t"],
                    "pass_id": pass_id,
                }
            )
        frames_json.write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        return {
            "ok": True,
            "count": len(entries),
            "added": len(document["times"]),
            "added_count": len(document["times"]),
            "frames_json": str(frames_json),
        }

    def _run_pass(
        self,
        *,
        video: Path,
        out_dir: Path,
        plan: Path,
        pass_id: str,
        extra_args: list[str] | None = None,
    ) -> tuple[int, dict]:
        argv = [
            "--video",
            str(video),
            "--out-dir",
            str(out_dir),
            "--plan",
            str(plan),
            "--pass-id",
            pass_id,
        ] + list(extra_args or [])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            refine,
            "invoke_frames",
            side_effect=self._accumulating_frames,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = refine.main(argv)
        lines = [
            line
            for line in stdout.getvalue().splitlines()
            if line.startswith(refine.RESULT_PREFIX)
        ]
        self.assertTrue(lines, stderr.getvalue())
        return code, json.loads(lines[-1][len(refine.RESULT_PREFIX) :])

    def test_budget_is_enforced_across_passes_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "input.mp4"
            video.write_bytes(b"x")
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"version": 1, "times": [i * 0.5 for i in range(200)]}),
                encoding="utf-8",
            )
            out_dir = root / "out"

            code1, result1 = self._run_pass(
                video=video, out_dir=out_dir, plan=plan, pass_id="r1",
                extra_args=["--max-passes", "3"],
            )
            code2, result2 = self._run_pass(
                video=video, out_dir=out_dir, plan=plan, pass_id="r2",
                extra_args=["--max-passes", "3"],
            )
            code3, result3 = self._run_pass(
                video=video, out_dir=out_dir, plan=plan, pass_id="r3",
                extra_args=["--max-passes", "3"],
            )

            self.assertEqual(code1, 0, result1)
            self.assertEqual(code2, 0, result2)
            self.assertEqual(result1["added_count"], 60)
            self.assertEqual(result2["added_count"], 60)
            # 第三轮被 120 帧累计上限拦截，而不是继续放行
            self.assertEqual(code3, 1)
            self.assertFalse(result3["ok"])
            self.assertIn("budget", result3["error"])
            manifest = json.loads(
                (out_dir / "manifest.json").read_text(encoding="utf-8")
            )
            passes = manifest["frames"]["passes"]
            self.assertEqual([item["pass_id"] for item in passes], ["r1", "r2"])
            self.assertEqual(manifest["frames"]["count"], 120)

    def test_missing_manifest_creates_minimal_manifest_with_base_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            entries = [
                {"file": "a.jpg", "t": 1.0, "pass_id": "base"},
                {"file": "b.jpg", "t": 2.0, "pass_id": "base"},
                {"file": "c.jpg", "t": 3.0, "pass_id": "r1"},
            ]
            (frames_dir / "frames.json").write_text(
                json.dumps(entries), encoding="utf-8"
            )

            remaining = refine.refinement_allowance(
                root,
                pass_id="r1",
                max_passes=2,
                max_total_extra=120,
            )

            manifest_path = root / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            # base_count 只统计非本 pass 的存量帧
            self.assertEqual(manifest["frames"]["base_count"], 2)
            self.assertEqual(manifest["frames"]["count"], 3)
            self.assertEqual(manifest["frames"]["passes"], [])
            self.assertEqual(remaining, 119)

    def test_max_extra_above_per_pass_limit_is_clamped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "input.mp4"
            video.write_bytes(b"x")
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"version": 1, "times": [i * 0.5 for i in range(200)]}),
                encoding="utf-8",
            )
            out_dir = root / "out"

            code, result = self._run_pass(
                video=video,
                out_dir=out_dir,
                plan=plan,
                pass_id="r1",
                extra_args=["--max-extra", "110"],
            )

            self.assertEqual(code, 0, result)
            self.assertEqual(result["requested_count"], refine.MAX_EXTRA_PER_PASS)
            self.assertEqual(result["added_count"], refine.MAX_EXTRA_PER_PASS)
            self.assertTrue(
                any("clamped" in warning for warning in result["warnings"]),
                result["warnings"],
            )
            document = json.loads(
                (out_dir / "refine_times_r1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(document["times"]), refine.MAX_EXTRA_PER_PASS)


if __name__ == "__main__":
    unittest.main()
