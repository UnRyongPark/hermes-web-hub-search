"""Unit tests for the usage log aggregation (no network, no Hermes imports)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import load_rows, summarize, render  # noqa: E402


def _row(day_offset: int, ok: bool = True, elapsed: float = 20.0, profile: str = "family",
         query: str = "q", now: datetime | None = None, error: str | None = None) -> dict:
    now = now or datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    row = {
        "ts": (now - timedelta(days=day_offset)).isoformat(timespec="seconds"),
        "profile": profile,
        "query": query,
        "limit": 5,
        "ok": ok,
        "results": 3 if ok else 0,
        "elapsed": elapsed,
    }
    if error:
        row["error"] = error
    return row


class PercentileTest(unittest.TestCase):
    def test_p50_p90(self) -> None:
        from cli import _percentile

        values = [float(i) for i in range(1, 11)]  # 1..10
        self.assertEqual(_percentile(values, 50), 5.0)
        self.assertEqual(_percentile(values, 90), 9.0)
        self.assertEqual(_percentile([], 50), 0.0)
        self.assertEqual(_percentile([7.0], 90), 7.0)


class SummarizeTest(unittest.TestCase):
    def test_empty(self) -> None:
        summary = summarize([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["success_rate"], 0.0)
        self.assertIn("No searches logged", render(summary))

    def test_counts_latency_and_rates(self) -> None:
        rows = [_row(0, True, 10.0), _row(0, True, 20.0), _row(1, False, 30.0, error="hub HTTP 502")]
        summary = summarize(rows, days=7)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertAlmostEqual(summary["success_rate"], 2 / 3)
        self.assertEqual(summary["latency"]["max"], 30.0)
        self.assertEqual(summary["latency"]["p50"], 20.0)
        self.assertEqual(summary["top_errors"], [("hub HTTP 502", 1)])

    def test_window_excludes_old_rows_but_totals_do_not(self) -> None:
        rows = [_row(0), _row(1), _row(30)]
        summary = summarize(rows, days=7)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(sum(summary["per_day"].values()), 2)
        self.assertEqual(len(summary["per_day"]), 2)

    def test_top_queries_and_profiles(self) -> None:
        rows = [_row(0, query="a"), _row(0, query="a"), _row(0, query="b", profile="po")]
        summary = summarize(rows)
        self.assertEqual(summary["top_queries"][0], ("a", 2))
        self.assertEqual(summary["per_profile"], {"family": 2, "po": 1})

    def test_render_contains_key_numbers(self) -> None:
        text = render(summarize([_row(0, True, 12.5)]))
        self.assertIn("1 searches", text)
        self.assertIn("100.0% success", text)
        self.assertIn("p50 12.5s", text)


class LoadRowsTest(unittest.TestCase):
    def test_skips_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            path.write_text(
                json.dumps(_row(0)) + "\n" + "not json\n\n" + json.dumps(_row(1)) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(load_rows(path)), 2)

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_rows(Path(tmp) / "nope.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
