from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import transcribe  # noqa: E402


class WindowHelpersTests(unittest.TestCase):
    def test_parse_window_accepts_clock_values(self) -> None:
        start, end, requested = transcribe.parse_window("01:02", "01:05.5")
        self.assertTrue(requested)
        self.assertEqual(start, 62.0)
        self.assertEqual(end, 65.5)

    def test_audio_command_uses_duration_and_preserves_argument_boundaries(self) -> None:
        command = transcribe.build_audio_extract_command(
            "ffmpeg",
            Path("input video.mp4"),
            Path("output audio.wav"),
            start=12.5,
            end=20.0,
        )
        self.assertEqual(command[0:4], ["ffmpeg", "-y", "-ss", "12.500"])
        self.assertEqual(command[command.index("-t") + 1], "7.500")
        self.assertIn("input video.mp4", command)
        self.assertEqual(command[-1], "output audio.wav")

    def test_offset_preserves_confidence_metadata(self) -> None:
        shifted = transcribe.offset_segments(
            [{
                "start": 1.25,
                "end": 2.5,
                "text": "sample",
                "avg_logprob": -0.25,
                "confidence": 0.7,
            }],
            10.0,
        )
        self.assertEqual(shifted[0]["start"], 11.25)
        self.assertEqual(shifted[0]["end"], 12.5)
        self.assertEqual(shifted[0]["avg_logprob"], -0.25)
        self.assertEqual(shifted[0]["confidence"], 0.7)

    def test_offset_segments_clamps_negative_timestamps(self) -> None:
        # B 站缓存 audio_minus_video_start=-0.023 会让首段平移出微负时间戳
        shifted = transcribe.offset_segments(
            [
                {"start": 0.0, "end": 1.5, "text": "first"},
                {"start": 0.5, "end": 2.0, "text": "second"},
            ],
            -0.023,
        )
        self.assertEqual(shifted[0]["start"], 0.0)
        self.assertAlmostEqual(shifted[0]["end"], 1.477)
        self.assertAlmostEqual(shifted[1]["start"], 0.477)
        self.assertAlmostEqual(shifted[1]["end"], 1.977)

    def test_faster_whisper_confidence_fields_are_serialisable(self) -> None:
        source = SimpleNamespace(
            avg_logprob=-0.4,
            no_speech_prob=0.02,
            compression_ratio=1.1,
            temperature=0.0,
        )
        fields = transcribe.asr_confidence_fields(source)
        self.assertAlmostEqual(fields["confidence"], math.exp(-0.4))
        record = transcribe.segment_json_record({
            "start": 0.0,
            "end": 1.0,
            "text": "hello",
            **fields,
        })
        self.assertEqual(record["avg_logprob"], -0.4)
        self.assertEqual(record["no_speech_prob"], 0.02)
        json.dumps(record)


class MainWindowTests(unittest.TestCase):
    @staticmethod
    def _last_result(stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.startswith("RESULT_JSON: ")]
        if not lines:
            raise AssertionError("missing RESULT_JSON")
        return json.loads(lines[-1][len("RESULT_JSON: "):])

    def test_video_window_is_shifted_back_to_source_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            video.write_bytes(b"placeholder")
            out_dir = root / "out"

            def fake_extract(media, output, start=None, end=None):
                self.assertEqual(media, video.resolve())
                self.assertEqual((start, end), (10.0, 20.0))
                output.mkdir(parents=True, exist_ok=True)
                audio = output / "audio.wav"
                audio.write_bytes(b"wav")
                return audio

            fake_segments = [{
                "start": 1.25,
                "end": 2.5,
                "text": "window text",
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
                "confidence": math.exp(-0.2),
            }]
            stdout = io.StringIO()
            with (
                mock.patch.object(transcribe, "extract_audio", side_effect=fake_extract),
                mock.patch.object(
                    transcribe,
                    "run_faster_whisper",
                    return_value=(fake_segments, "en", "cpu", "int8"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = transcribe.main([
                    "--video", str(video),
                    "--out-dir", str(out_dir),
                    "--start", "00:10",
                    "--end", "00:20",
                ])

            self.assertEqual(code, 0)
            transcript = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(transcript[0]["start"], 11.25)
            self.assertEqual(transcript[0]["end"], 12.5)
            self.assertEqual(transcript[0]["avg_logprob"], -0.2)
            result = self._last_result(stdout.getvalue())
            self.assertEqual(result["window"], {"start": 10.0, "end": 20.0})
            self.assertEqual(result["timeline"]["offset"], 10.0)

    def test_audio_without_window_keeps_legacy_direct_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "source.wav"
            audio.write_bytes(b"placeholder")
            out_dir = root / "out"

            with (
                mock.patch.object(transcribe, "extract_audio") as extract,
                mock.patch.object(
                    transcribe,
                    "run_faster_whisper",
                    return_value=([], "en", "cpu", "int8"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = transcribe.main([
                    "--audio", str(audio),
                    "--out-dir", str(out_dir),
                ])

            self.assertEqual(code, 0)
            extract.assert_not_called()

    def test_separate_audio_source_offset_aligns_to_video_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "source.wav"
            audio.write_bytes(b"placeholder")
            out_dir = root / "out"
            fake_segments = [{"start": 0.0, "end": 1.0, "text": "aligned"}]
            stdout = io.StringIO()

            with (
                mock.patch.object(transcribe, "extract_audio") as extract,
                mock.patch.object(
                    transcribe,
                    "run_faster_whisper",
                    return_value=(fake_segments, "en", "cpu", "int8"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = transcribe.main([
                    "--audio", str(audio),
                    "--out-dir", str(out_dir),
                    "--source-offset", "0.05",
                ])

            self.assertEqual(code, 0)
            extract.assert_not_called()
            transcript = json.loads(
                (out_dir / "transcript.json").read_text(encoding="utf-8")
            )
            self.assertEqual(transcript[0]["start"], 0.05)
            self.assertEqual(transcript[0]["end"], 1.05)
            result = self._last_result(stdout.getvalue())
            self.assertEqual(result["timeline"]["offset"], 0.05)
            self.assertEqual(result["timeline"]["source_offset"], 0.05)

    def test_source_window_is_inverse_mapped_before_audio_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "source.wav"
            audio.write_bytes(b"placeholder")
            out_dir = root / "out"

            def fake_extract(media, output, start=None, end=None):
                self.assertEqual(media, audio.resolve())
                self.assertAlmostEqual(start, 9.95)
                self.assertAlmostEqual(end, 19.95)
                output.mkdir(parents=True, exist_ok=True)
                wav = output / "audio.wav"
                wav.write_bytes(b"wav")
                return wav

            stdout = io.StringIO()
            with (
                mock.patch.object(transcribe, "extract_audio", side_effect=fake_extract),
                mock.patch.object(
                    transcribe,
                    "run_faster_whisper",
                    return_value=(
                        [{"start": 0.0, "end": 1.0, "text": "window"}],
                        "en",
                        "cpu",
                        "int8",
                    ),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = transcribe.main([
                    "--audio", str(audio),
                    "--out-dir", str(out_dir),
                    "--start", "10",
                    "--end", "20",
                    "--source-offset", "0.05",
                ])

            self.assertEqual(code, 0)
            transcript = json.loads(
                (out_dir / "transcript.json").read_text(encoding="utf-8")
            )
            self.assertEqual(transcript[0]["start"], 10.0)
            self.assertEqual(transcript[0]["end"], 11.0)
            result = self._last_result(stdout.getvalue())
            self.assertEqual(result["timeline"]["offset"], 10.0)
            self.assertAlmostEqual(result["timeline"]["media_seek_start"], 9.95)

    def test_caption_window_filters_without_adding_seek_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            captions = root / "captions.vtt"
            captions.write_text(
                "WEBVTT\n\n"
                "00:00:08.000 --> 00:00:09.000\ninside\n\n"
                "00:00:20.000 --> 00:00:21.000\noutside\n",
                encoding="utf-8",
            )
            out_dir = root / "out"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = transcribe.main([
                    "--vtt", str(captions),
                    "--out-dir", str(out_dir),
                    "--start", "5",
                    "--end", "10",
                ])

            transcript = json.loads(
                (out_dir / "transcript.json").read_text(encoding="utf-8")
            )
            result = self._last_result(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(transcript[0]["start"], 8.0)
        self.assertEqual(len(transcript), 1)
        self.assertEqual(result["timeline"]["offset"], 0.0)
        self.assertIsNone(result["timeline"]["media_seek_start"])


if __name__ == "__main__":
    unittest.main()
