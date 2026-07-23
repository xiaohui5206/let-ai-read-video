import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import watch  # noqa: E402


class WatchHelpersTests(unittest.TestCase):
    def test_display_arg_redacts_url_credentials_and_query(self):
        value = "https://user:secret@example.com:8443/video?id=1&token=abc#part"
        self.assertEqual(
            watch._display_arg(value),
            "https://example.com:8443/video",
        )

    def test_display_arg_preserves_local_path(self):
        value = r"C:\Videos\meeting.mp4"
        self.assertEqual(watch._display_arg(value), value)

    def test_atomic_json_write_replaces_complete_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text('{"old": true}', encoding="utf-8")

            watch._write_json_atomic(path, {"schema_version": 2, "ok": True})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema_version": 2, "ok": True},
            )
            self.assertFalse(path.with_name("manifest.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
