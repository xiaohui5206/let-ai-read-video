from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DELIVER_PATH = ROOT / "scripts" / "deliver.py"
SPEC = importlib.util.spec_from_file_location("video_watch_deliver", DELIVER_PATH)
assert SPEC is not None and SPEC.loader is not None
deliver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deliver
SPEC.loader.exec_module(deliver)


class SanitizeNameTests(unittest.TestCase):
    def test_removes_halfwidth_illegal_characters(self) -> None:
        self.assertEqual(
            deliver._sanitize_name('a/b\\c:d*e?f"g<h>i|j'),
            "abcdefghij",
        )

    def test_removes_control_characters(self) -> None:
        self.assertEqual(deliver._sanitize_name("a\x00b\x1fc\x7fd"), "abcd")

    def test_trims_leading_and_trailing_whitespace(self) -> None:
        self.assertEqual(deliver._sanitize_name("  标题  "), "标题")

    def test_truncates_to_sixty_chars_by_default(self) -> None:
        long_title = "题" * 100
        self.assertEqual(len(deliver._sanitize_name(long_title)), 60)
        self.assertEqual(deliver._sanitize_name(long_title), "题" * 60)

    def test_truncation_strips_trailing_whitespace_again(self) -> None:
        # 第 60 字符处恰好截在空格上时，尾部空格应再被去掉
        title = "字" * 59 + " 结尾"
        self.assertEqual(deliver._sanitize_name(title), "字" * 59)

    def test_custom_max_len(self) -> None:
        self.assertEqual(deliver._sanitize_name("abcdefgh", max_len=3), "abc")

    def test_empty_title_falls_back(self) -> None:
        self.assertEqual(deliver._sanitize_name(""), "video")
        self.assertEqual(deliver._sanitize_name(None), "video")
        # 全是非法字符净化后为空，同样兜底
        self.assertEqual(deliver._sanitize_name('///***???'), "video")
        self.assertEqual(deliver._sanitize_name("   "), "video")

    def test_custom_fallback(self) -> None:
        self.assertEqual(deliver._sanitize_name("", fallback="未命名"), "未命名")


class FmtHmsTests(unittest.TestCase):
    def test_seconds_become_mm_ss_under_one_hour(self) -> None:
        # 遵循仓库 common.fmt_ts 惯例：不足一小时用 MM:SS 而非 00:MM:SS
        self.assertEqual(deliver._fmt_hms(0), "00:00")
        self.assertEqual(deliver._fmt_hms(61.2), "01:01")
        self.assertEqual(deliver._fmt_hms(599), "09:59")

    def test_full_hours_use_hh_mm_ss(self) -> None:
        self.assertEqual(deliver._fmt_hms(3600), "01:00:00")
        self.assertEqual(deliver._fmt_hms(3723), "01:02:03")
        self.assertEqual(deliver._fmt_hms(6119.27), "01:41:59")

    def test_seconds_round_to_integer(self) -> None:
        self.assertEqual(deliver._fmt_hms(89.6), "01:30")
        self.assertEqual(deliver._fmt_hms(3599.6), "01:00:00")

    def test_invalid_input_falls_back_to_zero(self) -> None:
        self.assertEqual(deliver._fmt_hms(None), "00:00")
        self.assertEqual(deliver._fmt_hms("abc"), "00:00")


class FrameTimeTests(unittest.TestCase):
    def test_actual_t_wins_then_requested_then_t(self) -> None:
        frame = {"t": 1.0, "requested_t": 2.0, "actual_t": 3.0}
        self.assertEqual(deliver._frame_time(frame), 3.0)
        self.assertEqual(deliver._frame_time({"t": 1.0, "requested_t": 2.0}), 2.0)
        self.assertEqual(deliver._frame_time({"t": 1.0}), 1.0)
        self.assertEqual(deliver._frame_time({}), 0.0)

    def test_zero_actual_t_is_not_skipped(self) -> None:
        # actual_t = 0.0 是合法值，不能被当成缺失而落到 requested_t
        frame = {"actual_t": 0.0, "requested_t": 5.0, "t": 6.0}
        self.assertEqual(deliver._frame_time(frame), 0.0)


if __name__ == "__main__":
    unittest.main()
