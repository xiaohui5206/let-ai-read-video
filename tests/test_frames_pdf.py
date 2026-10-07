# -*- coding: utf-8 -*-
"""make_frames_pdf.py 测试：真实 JPEG（ffmpeg 现场生成）→ PDF 结构与 RESULT_JSON 契约。

无网络；帧图用 `ffmpeg -f lavfi -i color=... -frames:v 1` 现场生成，
ffmpeg 路径走 common.find_tool（tools/ 便携版优先），找不到则 skipTest。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402
import make_frames_pdf as mkpdf  # noqa: E402

FFMPEG = common.find_tool("ffmpeg")


def _make_jpeg(path, color="red", size="64x48"):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color={color}:{size}", "-frames:v", "1", str(path)],
        check=True, timeout=60)


def _make_run(root):
    """3 张真帧 + frames.json（乱序写入验证排序；含 1 张 dropped:true 验证跳过）。"""
    run_dir = root / "run1"
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True)
    # (文件名, actual_t, 颜色, 是否带 actual_t 字段)
    specs = [
        ("0002_t3601.0.jpg", 3601.0, "blue", True),
        ("0000_t0000.0.jpg", 0.0, "red", False),     # 只有 t，验证 actual_t 缺省回退
        ("0001_t0061.5.jpg", 61.5, "green", True),
    ]
    entries = []
    for name, t, color, has_actual in specs:
        _make_jpeg(frames_dir / name, color)
        entry = {"file": name, "t": t, "pass_id": "base"}
        if has_actual:
            entry["actual_t"] = t
        entries.append(entry)
    _make_jpeg(frames_dir / "0003_t0099.0.jpg", "yellow")
    entries.append({"file": "0003_t0099.0.jpg", "t": 99.0, "actual_t": 99.0,
                    "dropped": True})   # 为去重功能预留：导出不计入
    (frames_dir / "frames.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _assert_xref_offsets_precise(testcase, data):
    """结构性自检：xref 表每个偏移处确实以 `N 0 obj` 开头。"""
    m = re.search(rb"\nxref\n0 (\d+)\n", data)
    testcase.assertIsNotNone(m, "缺 xref 段")
    size = int(m.group(1))
    pos = m.end()
    for i in range(size):
        line_end = data.index(b"\n", pos)
        line = data[pos:line_end]
        pos = line_end + 1
        testcase.assertEqual(len(line), 19, f"xref 行长度非法: {line!r}")
        offset = int(line[:10])
        flag = line[17:19]   # "f "（free）或 "n "（in-use）
        if i == 0:
            testcase.assertEqual(flag, b"f ")
            continue
        testcase.assertEqual(flag, b"n ")
        head = f"{i} 0 obj".encode("ascii")
        testcase.assertEqual(data[offset:offset + len(head)], head,
                             f"xref 偏移 {offset} 处不是对象 {i}")


@unittest.skipUnless(FFMPEG, "ffmpeg 不可用，跳过 PDF 结构测试")
class FramesPdfTests(unittest.TestCase):

    def test_pdf_structure_and_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _make_run(Path(tmp))
            result = mkpdf.build_from_run_dir(run_dir, run_dir / "关键帧.pdf")
            data = Path(result["pdf"]).read_bytes()

        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"trailer", data)
        self.assertIn(b"startxref", data)
        # 3 帧原样嵌入（dropped 帧未计入）
        self.assertEqual(data.count(b"/DCTDecode"), 3)
        # caption：actual_t 截断取整，MM:SS / H:MM:SS；乱序写入已按时间排序
        self.assertIn(b"#1  t=00:00", data)
        self.assertIn(b"#2  t=01:01", data)
        self.assertIn(b"#3  t=1:00:01", data)
        self.assertNotIn(b"t=01:39", data)   # dropped 帧（t=99.0）未计入
        self.assertEqual(result["frames_included"], 3)
        self.assertEqual(result["frames_skipped"], 1)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["pdf"].endswith("关键帧.pdf"))

    def test_xref_offsets_precise(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _make_run(Path(tmp))
            result = mkpdf.build_from_run_dir(run_dir, run_dir / "关键帧.pdf")
            _assert_xref_offsets_precise(self, Path(result["pdf"]).read_bytes())

    def test_multi_page_layout(self):
        # 7 帧 → 2 页（每页 2列×3行=6 帧）
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_multi"
            frames_dir = run_dir / "frames"
            frames_dir.mkdir(parents=True)
            entries = []
            for i in range(7):
                name = f"{i:04d}_t{i * 10:08.1f}.jpg"
                _make_jpeg(frames_dir / name, "red")
                entries.append({"file": name, "t": float(i * 10), "actual_t": float(i * 10)})
            (frames_dir / "frames.json").write_text(json.dumps(entries), encoding="utf-8")
            result = mkpdf.build_from_run_dir(run_dir, run_dir / "关键帧.pdf")
            self.assertEqual(result["pages"], 2)
            self.assertEqual(result["frames_included"], 7)
            data = Path(result["pdf"]).read_bytes()
            self.assertEqual(data.count(b"/DCTDecode"), 7)
            self.assertEqual(data.count(b"/Type /Page "), 2)   # 两个 Page 对象
            self.assertIn(b"/Count 2", data)
            _assert_xref_offsets_precise(self, data)

    def test_jpeg_size_parses_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "x.jpg"
            _make_jpeg(img, "red", "64x48")
            self.assertEqual(mkpdf.jpeg_size(img.read_bytes()), (64, 48))

    def test_jpeg_size_rejects_non_jpeg(self):
        with self.assertRaises(ValueError):
            mkpdf.jpeg_size(b"not a jpeg at all")

    def test_caption_time_format(self):
        self.assertEqual(mkpdf.fmt_caption_t(0.0), "00:00")
        self.assertEqual(mkpdf.fmt_caption_t(61.5), "01:01")    # 截断取整
        self.assertEqual(mkpdf.fmt_caption_t(3601.0), "1:00:01")
        self.assertEqual(mkpdf.fmt_caption_t(754.9), "12:34")


class FramesPdfCliTests(unittest.TestCase):
    """CLI 契约：stdout 末行 RESULT_JSON；失败 ok:false + 退出码 1。"""

    def _run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "make_frames_pdf.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120)

    def _parse_result(self, proc):
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("RESULT_JSON: "):
                return json.loads(line[len("RESULT_JSON: "):])
        return None

    @unittest.skipUnless(FFMPEG, "ffmpeg 不可用，跳过 CLI 测试")
    def test_main_ok_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _make_run(Path(tmp))
            proc = self._run_cli("--run-dir", str(run_dir))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = self._parse_result(proc)
            self.assertTrue(result["ok"])
            self.assertEqual(result["pages"], 1)
            self.assertEqual(result["frames_included"], 3)
            pdf = Path(result["pdf"])
            self.assertTrue(pdf.is_file())
            self.assertEqual(pdf.name, "关键帧.pdf")

    def test_missing_frames_json_fails_with_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_cli("--run-dir", tmp)
            self.assertEqual(proc.returncode, 1)
            result = self._parse_result(proc)
            self.assertIsNotNone(result)
            self.assertFalse(result["ok"])
            self.assertIn("frames.json", result["error"])

    def test_missing_run_dir_fails_with_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_cli("--run-dir", str(Path(tmp) / "不存在"))
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(self._parse_result(proc)["ok"])


if __name__ == "__main__":
    unittest.main()
