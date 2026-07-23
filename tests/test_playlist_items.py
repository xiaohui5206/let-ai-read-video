import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import download  # noqa: E402
import probe  # noqa: E402


def make_playlist_info(count: int) -> dict:
    """构造 yt_dlp 风格的播放列表元数据：第 i 集时长 100+i 秒。"""
    return {
        "title": "测试合集",
        "entries": [
            {
                "id": f"ep{i}",
                "title": f"第{i}集",
                "duration": 100 + i,
                "formats": [{
                    "vcodec": "h264", "acodec": "aac",
                    "width": 1920, "height": 1080, "fps": 30,
                }],
            }
            for i in range(1, count + 1)
        ],
    }


class FakeYoutubeDL:
    """伪造 yt_dlp.YoutubeDL：extract_info 返回固定元数据；

    process_ie_result 在 outtmpl 目录落一个假媒体文件，模拟真实下载。
    """

    def __init__(self, opts, info):
        self._opts = opts or {}
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return self._info

    def process_ie_result(self, ie_result, download=True):
        out_dir = Path(self._opts["outtmpl"]).parent
        media = out_dir / "fake-media.mp4"
        media.write_bytes(b"fake")
        return {
            "title": ie_result.get("title"),
            "duration": ie_result.get("duration"),
            "requested_downloads": [{"filepath": str(media)}],
        }


def patch_yt_dlp(info):
    """把 sys.modules 里的 yt_dlp 换成返回固定元数据的假模块。"""
    fake = types.SimpleNamespace(
        YoutubeDL=lambda opts: FakeYoutubeDL(opts, info),
    )
    return mock.patch.dict(sys.modules, {"yt_dlp": fake})


def run_main(module, argv):
    """运行 CLI main，捕获 stdout/stderr 并解析末行 RESULT_JSON。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = module.main(argv)
    result_line = [
        line for line in stdout.getvalue().splitlines()
        if line.startswith("RESULT_JSON: ")
    ][-1]
    result = json.loads(result_line[len("RESULT_JSON: "):])
    return code, result, stdout.getvalue(), stderr.getvalue()


class ProbePlaylistTests(unittest.TestCase):
    def test_probe_52_entries_reports_full_playlist(self):
        with patch_yt_dlp(make_playlist_info(52)):
            code, result, stdout, _ = run_main(
                probe, ["--input", "https://example.com/playlist"])

        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        playlist = result["playlist"]
        self.assertEqual(playlist["count"], 52)
        self.assertEqual(len(playlist["items"]), 52)
        first = playlist["items"][0]
        self.assertEqual(first["index"], 1)
        self.assertEqual(first["title"], "第1集")
        self.assertEqual(first["duration"], 101.0)
        self.assertEqual(playlist["items"][-1]["index"], 52)
        # 日志只打印前 10 集清单，超出打 ...
        self.assertIn("  10. 第10集", stdout)
        self.assertNotIn("  11. 第11集", stdout)
        self.assertIn("  ...", stdout)

    def test_probe_single_video_playlist_is_null(self):
        info = {
            "title": "单视频",
            "duration": 12.0,
            "formats": [{
                "vcodec": "h264", "acodec": "aac",
                "width": 640, "height": 360, "fps": 30,
            }],
        }
        with patch_yt_dlp(info):
            code, result, _, _ = run_main(
                probe, ["--input", "https://example.com/video"])

        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["playlist"])


class DownloadItemTests(unittest.TestCase):
    def run_download(self, out_dir, item, count=10):
        with patch_yt_dlp(make_playlist_info(count)):
            return run_main(download, [
                "--url", "https://example.com/playlist",
                "--out-dir", str(out_dir),
                "--item", item,
            ])

    def test_item_range_downloads_first_and_reports_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _, _ = self.run_download(tmp, "3-7")

            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["requested_items"], [3, 4, 5, 6, 7])
            self.assertEqual(result["downloaded_item"], 3)
            self.assertTrue(Path(result["video_path"]).is_file())

    def test_item_single_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _, _ = self.run_download(tmp, "3")

            self.assertEqual(code, 0)
            self.assertEqual(result["requested_items"], [3])
            self.assertEqual(result["downloaded_item"], 3)

    def test_item_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _, _ = self.run_download(tmp, "all")

            self.assertEqual(code, 0)
            self.assertEqual(result["requested_items"], list(range(1, 11)))
            self.assertEqual(result["downloaded_item"], 1)

    def test_item_illegal_specs_fail_with_range_hint(self):
        for bad in ("abc", "7-3", "0", "2-999"):
            with self.subTest(item=bad), tempfile.TemporaryDirectory() as tmp:
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    patch_yt_dlp(make_playlist_info(10)),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    download.main([
                        "--url", "https://example.com/playlist",
                        "--out-dir", tmp,
                        "--item", bad,
                    ])

                self.assertEqual(raised.exception.code, 1)
                result_line = [
                    line for line in stdout.getvalue().splitlines()
                    if line.startswith("RESULT_JSON: ")
                ][-1]
                result = json.loads(result_line[len("RESULT_JSON: "):])
                self.assertFalse(result["ok"])
                self.assertIn("集数范围", result["error"])
                self.assertIn("ERROR:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
