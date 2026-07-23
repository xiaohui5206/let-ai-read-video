import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402


class UrlPrivacyTests(unittest.TestCase):
    def test_redact_url_removes_credentials_query_and_fragment(self):
        raw = "https://user:secret@example.com:8443/video?id=1&token=abc#part"

        safe = common.redact_url(raw)

        self.assertEqual(safe, "https://example.com:8443/video")
        self.assertNotIn("secret", safe)
        self.assertNotIn("token", safe)

    def test_redact_text_urls_scrubs_embedded_signed_url(self):
        raw = (
            "download failed for "
            "https://cdn.example/video.mp4?signature=private&expires=1"
        )

        safe = common.redact_text_urls(raw)

        self.assertEqual(
            safe,
            "download failed for https://cdn.example/video.mp4",
        )


if __name__ == "__main__":
    unittest.main()
