# -*- coding: utf-8 -*-
"""watch.py 多集编排（run_multi_episodes）回归测试。

覆盖：
- 区间请求逐集执行，run 目录按 pNN 命名；
- 单集失败记 error 后继续，聚合 RESULT 正确（部分失败 ok 仍为 true）。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watch  # noqa: E402


def _args(out_dir: str) -> Namespace:
    return Namespace(step_timeout=0, input="https://example.com/v",
                     force_whisper=False, no_transcribe=False,
                     out_dir=out_dir)


def _base(tmp: str) -> dict:
    return {"run_dir": Path(tmp) / "first", "title": "测试课程",
            "duration": 100.0, "has_video": True, "has_audio": True,
            "start_s": None, "end_s": None}


def _run(args, base, items, fail_on=()):
    """跑 run_multi_episodes：mock run_step / process_one，返回聚合 RESULT。"""
    download_items = []

    def fake_run_step(script, cli_args, label, fatal=True, timeout=None,
                      raise_on_fail=False):
        if script == "download.py":
            download_items.append(int(cli_args[cli_args.index("--item") + 1]))
            return {"video_path": "e.mp4", "title": "T",
                    "duration": 100.0, "captions": []}
        return {}

    def fake_process_one(a, ctx):
        # 通过 run_dir 名称反推第几集（pNN 或首集）
        name = Path(ctx["run_dir"]).name
        item = int(name[1:3]) if name.startswith("p") else items[0]
        if item in fail_on:
            raise watch.EpisodeFailed(f"第 {item} 集模拟失败")
        return {"transcript_txt": "t.txt", "frames_json": "f.json",
                "review_json": "r.json"}

    buf = io.StringIO()
    with mock.patch.object(watch, "run_step", side_effect=fake_run_step), \
         mock.patch.object(watch, "process_one", side_effect=fake_process_one), \
         contextlib.redirect_stdout(buf):
        watch.run_multi_episodes(args, base, {"video_path": "first.mp4",
                                              "title": "T", "duration": 100.0,
                                              "captions": []}, items)
    line = [ln for ln in buf.getvalue().splitlines()
            if ln.startswith(watch.RESULT_PREFIX)][-1]
    return json.loads(line[len(watch.RESULT_PREFIX):]), download_items


class MultiEpisodeTests(unittest.TestCase):
    def test_range_runs_each_episode_with_pnn_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, downloads = _run(_args(tmp), _base(tmp), [3, 4, 5, 6, 7])
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["succeeded"], 5)
        self.assertEqual(result["failed"], 0)
        # 首集复用主 run 目录，其余集独立下载且 run 目录带 pNN
        self.assertEqual(downloads, [4, 5, 6, 7])
        names = [Path(e["run_dir"]).name for e in result["episodes"]]
        self.assertEqual(names[1:], ["p04", "p05", "p06", "p07"])
        self.assertTrue(all(e["ok"] for e in result["episodes"]))

    def test_failed_episode_does_not_break_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _run(_args(tmp), _base(tmp), [3, 4, 5], fail_on={4})
        self.assertTrue(result["ok"])           # 部分失败整体仍 ok
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 1)
        ep4 = result["episodes"][1]
        self.assertFalse(ep4["ok"])
        self.assertIn("模拟失败", ep4["error"])
        self.assertTrue(result["episodes"][2]["ok"])  # 失败后续集继续


if __name__ == "__main__":
    unittest.main()
