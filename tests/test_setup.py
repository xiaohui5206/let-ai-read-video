import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = ROOT / "scripts" / "setup.py"
SPEC = importlib.util.spec_from_file_location("video_watch_setup", SETUP_PATH)
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)


class SetupProfilesTests(unittest.TestCase):
    def test_local_profile_does_not_require_ytdlp(self):
        tools = {
            "ffmpeg": {"found": True},
            "ffprobe": {"found": True},
            "yt-dlp": {"found": False},
        }
        packages = {
            "faster-whisper": {"installed": True},
            "yt-dlp": {"installed": False},
        }

        self.assertEqual(setup.build_missing(tools, packages, "local"), [])
        self.assertEqual(
            setup.build_missing(tools, packages, "all"),
            ["yt-dlp", "pip:yt-dlp"],
        )


if __name__ == "__main__":
    unittest.main()
