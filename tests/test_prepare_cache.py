from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import prepare_cache  # noqa: E402


def make_candidate(
    path: str,
    *,
    duration: float,
    start: float = 0.0,
    width: int | None = None,
    height: int | None = None,
    video_bitrate: int = 0,
    audio_bitrate: int = 0,
) -> dict:
    video = None
    audio = None
    if width is not None and height is not None:
        video = {
            "index": 0,
            "type": "video",
            "codec": "h264",
            "width": width,
            "height": height,
            "bitrate": video_bitrate,
            "start_time": start,
            "duration": duration,
        }
    if audio_bitrate:
        audio = {
            "index": 1 if video else 0,
            "type": "audio",
            "codec": "aac",
            "bitrate": audio_bitrate,
            "channels": 2,
            "sample_rate": 48000,
            "start_time": start,
            "duration": duration,
        }
    return {
        "probe_ok": True,
        "path": path,
        "source_path": path + ".m4s",
        "has_video": video is not None,
        "has_audio": audio is not None,
        "duration": duration,
        "start_time": start,
        "bitrate": max(video_bitrate, audio_bitrate),
        "width": width,
        "height": height,
        "video": video,
        "audio": audio,
        "stream_details": [s for s in (video, audio) if s],
    }


class ProbeAndSelectionTests(unittest.TestCase):
    def test_probe_payload_keeps_combined_stream_metadata(self) -> None:
        parsed = prepare_cache.parse_probe_payload({
            "format": {
                "duration": "12.5",
                "start_time": "0.125",
                "bit_rate": "2400000",
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "bit_rate": "2200000",
                    "start_time": "0.125",
                    "duration": "12.5",
                    "avg_frame_rate": "30/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "bit_rate": "192000",
                    "sample_rate": "48000",
                    "channels": 2,
                    "start_time": "0.150",
                    "duration": "12.45",
                },
            ],
        })
        self.assertTrue(parsed["has_video"])
        self.assertTrue(parsed["has_audio"])
        self.assertEqual((parsed["width"], parsed["height"]), (1920, 1080))
        self.assertEqual(parsed["video"]["bitrate"], 2200000)
        self.assertEqual(parsed["audio"]["start_time"], 0.15)

    def test_best_video_is_paired_with_closest_duration_audio(self) -> None:
        low_video = make_candidate(
            "720.mp4", duration=100.0, width=1280, height=720, video_bitrate=1500000
        )
        best_video = make_candidate(
            "1080.mp4", duration=99.8, width=1920, height=1080, video_bitrate=3000000
        )
        high_bitrate_far_audio = make_candidate(
            "far.m4a", duration=95.0, audio_bitrate=320000
        )
        closest_audio = make_candidate(
            "near.m4a", duration=99.75, audio_bitrate=128000
        )

        video, audio = prepare_cache.select_stream_pair([
            low_video, best_video, high_bitrate_far_audio, closest_audio
        ])
        self.assertIs(video, best_video)
        self.assertIs(audio, closest_audio)

    def test_combined_av_file_can_supply_both_sides(self) -> None:
        combined = make_candidate(
            "combined.mp4",
            duration=30.0,
            width=1920,
            height=1080,
            video_bitrate=2500000,
            audio_bitrate=192000,
        )
        video, audio = prepare_cache.select_stream_pair([combined])
        self.assertIs(video, combined)
        self.assertIs(audio, combined)
        timeline = prepare_cache.build_timeline_metadata(video, audio)
        self.assertEqual(timeline["audio_minus_video_start"], 0.0)


class PathsAndMainTests(unittest.TestCase):
    def test_recursive_duplicate_stems_get_distinct_fixed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            first = source / "a" / "stream.m4s"
            second = source / "b" / "stream.m4s"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            payload = b"000000000" + b"\x00\x00\x00\x18ftypisom" + b"payload"
            first.write_bytes(payload)
            second.write_bytes(payload)
            fixed_root = root / "fixed"

            first_dst = prepare_cache.fixed_copy_path(first, source, fixed_root)
            second_dst = prepare_cache.fixed_copy_path(second, source, fixed_root)
            self.assertNotEqual(first_dst, second_dst)
            prepare_cache.fix_m4s(first, first_dst)
            prepare_cache.fix_m4s(second, second_dst)
            self.assertTrue(first_dst.is_file())
            self.assertTrue(second_dst.is_file())
            self.assertTrue(first_dst.read_bytes().startswith(b"\x00\x00\x00\x18ftyp"))

    def test_main_outputs_selected_streams_and_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cache"
            video_file = source / "video" / "stream.m4s"
            audio_file = source / "audio" / "stream.m4s"
            video_file.parent.mkdir(parents=True)
            audio_file.parent.mkdir(parents=True)
            payload = b"000000000" + b"\x00\x00\x00\x18ftypisom" + b"payload"
            video_file.write_bytes(payload)
            audio_file.write_bytes(payload)
            (source / "videoInfo.json").write_text(
                json.dumps({"title": "sample", "duration": 10.0}),
                encoding="utf-8",
            )
            out_dir = root / "out"

            def fake_probe(_ffprobe, path):
                if "video" in path.parts:
                    return make_candidate(
                        str(path),
                        duration=10.0,
                        start=0.0,
                        width=1920,
                        height=1080,
                        video_bitrate=2500000,
                    )
                return make_candidate(
                    str(path),
                    duration=9.98,
                    start=0.05,
                    audio_bitrate=192000,
                )

            stdout = io.StringIO()
            with (
                mock.patch.object(prepare_cache, "find_tool", return_value="ffprobe"),
                mock.patch.object(prepare_cache, "probe_streams", side_effect=fake_probe),
                contextlib.redirect_stdout(stdout),
            ):
                code = prepare_cache.main([
                    "--input", str(source),
                    "--out-dir", str(out_dir),
                ])

            self.assertEqual(code, 0)
            result_line = [
                line for line in stdout.getvalue().splitlines()
                if line.startswith("RESULT_JSON: ")
            ][-1]
            result = json.loads(result_line[len("RESULT_JSON: "):])
            self.assertEqual(result["streams"], {"video": 1, "audio": 1})
            self.assertEqual(result["selected_streams"]["video"]["width"], 1920)
            self.assertEqual(result["selected_streams"]["audio"]["bitrate"], 192000)
            self.assertAlmostEqual(result["timeline"]["audio_minus_video_start"], 0.05)
            self.assertEqual(len(result["stream_metadata"]), 2)
            self.assertNotEqual(result["video_path"], result["audio_path"])

    def test_main_rejects_cache_without_decodable_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cache"
            source.mkdir()
            cache_file = source / "broken.m4s"
            cache_file.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"payload")
            invalid_probe = {
                "probe_ok": False,
                "has_video": False,
                "has_audio": False,
                "duration": None,
                "start_time": None,
                "bitrate": None,
                "width": None,
                "height": None,
                "video": None,
                "audio": None,
                "stream_details": [],
            }

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(prepare_cache, "find_tool", return_value="ffprobe"),
                mock.patch.object(prepare_cache, "probe_streams", return_value=invalid_probe),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                prepare_cache.main([
                    "--input", str(source),
                    "--out-dir", str(root / "out"),
                ])

        self.assertEqual(raised.exception.code, 1)
        result_line = [
            line for line in stdout.getvalue().splitlines()
            if line.startswith("RESULT_JSON: ")
        ][-1]
        result = json.loads(result_line[len("RESULT_JSON: "):])
        self.assertFalse(result["ok"])
        self.assertIn("可解码", result["error"])


if __name__ == "__main__":
    unittest.main()
