"""Unit tests for the prose-salvage path (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # provider.py imports the Hermes agent package
    import agent.web_search_provider  # noqa: F401
except ImportError:  # pragma: no cover - allow running outside a Hermes checkout
    sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))

from provider import _salvage_rows  # noqa: E402


class SalvageTest(unittest.TestCase):
    def test_recovers_cited_markdown_link(self) -> None:
        text = (
            "우리카드 **카드의정석2 SOLID** 연회비는 **15,000원**입니다. "
            "([hankyung.com](https://www.hankyung.com/article/2026101379231?utm_source=openai))"
        )
        rows = _salvage_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.hankyung.com/article/2026101379231")
        self.assertIn("연회비", rows[0]["snippet"])

    def test_recovers_bare_url_and_ignores_link_dupes(self) -> None:
        text = "See [card-gorilla](https://m.card-gorilla.com/contents/detail/406) or https://m.card-gorilla.com/contents/detail/406."
        rows = _salvage_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "card-gorilla")

    def test_multiple_links_keep_order_and_titles(self) -> None:
        text = (
            '[카드고릴라](https://m.card-gorilla.com/contents/detail/406) 에서 비교. '
            "그리고 [KB공식](https://card.kbcard.com/CRD/DVIEW/HCAMCXPRICAC0076) 도 있다."
        )
        rows = _salvage_rows(text)
        self.assertEqual([r["url"] for r in rows], [
            "https://m.card-gorilla.com/contents/detail/406",
            "https://card.kbcard.com/CRD/DVIEW/HCAMCXPRICAC0076",
        ])
        self.assertEqual(rows[0]["title"], "카드고릴라")

    def test_prose_without_links_yields_nothing(self) -> None:
        self.assertEqual(_salvage_rows("검색 결과를 찾을 수 없습니다."), [])
        self.assertEqual(_salvage_rows(""), [])

    def test_bare_url_gets_host_as_title(self) -> None:
        rows = _salvage_rows("참고: https://www.busan.go.kr/tour/haeundae")
        self.assertEqual(rows[0]["title"], "www.busan.go.kr")


if __name__ == "__main__":
    unittest.main()
