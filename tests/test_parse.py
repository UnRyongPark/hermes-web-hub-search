"""Unit tests for the hub-search response mapping (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # provider.py imports the Hermes agent package
    import agent.web_search_provider  # noqa: F401
except ImportError:  # pragma: no cover - allow running outside a Hermes checkout
    sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))

from provider import _clean_url, _parse_array  # noqa: E402


class CleanUrlTest(unittest.TestCase):
    def test_strips_utm_source(self) -> None:
        self.assertEqual(
            _clean_url("https://example.com/docs/?utm_source=openai"),
            "https://example.com/docs/",
        )

    def test_keeps_real_params(self) -> None:
        self.assertEqual(
            _clean_url("https://example.com/x?a=1&utm_medium=email&b=2"),
            "https://example.com/x?a=1&b=2",
        )


class ParseArrayTest(unittest.TestCase):
    def test_plain_array(self) -> None:
        rows = _parse_array('[{"title": "T", "url": "https://a.dev", "snippet": "S"}]')
        self.assertEqual(rows, [{"url": "https://a.dev", "title": "T", "snippet": "S"}])

    def test_markdown_fence(self) -> None:
        rows = _parse_array('```json\n[{"title": "T", "url": "https://a.dev", "snippet": "S"}]\n```')
        self.assertEqual(len(rows), 1)

    def test_surrounded_by_prose(self) -> None:
        rows = _parse_array('Here you go: [{"title": "T", "url": "https://a.dev", "snippet": "S"}] done')
        self.assertEqual(len(rows), 1)

    def test_drops_non_http_and_garbage(self) -> None:
        self.assertEqual(_parse_array('[{"url": "ftp://x"}, {"nope": 1}]'), [])
        self.assertEqual(_parse_array("no json here"), [])


if __name__ == "__main__":
    unittest.main()
