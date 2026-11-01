"""Log rotation tests: monthly file naming, age-based pruning, multi-file loading."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # provider.py imports the Hermes agent package
    import agent.web_search_provider  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))

import provider  # noqa: E402
from cli import load_rows  # noqa: E402


class RotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _touch_month(self, year: int, month: int) -> Path:
        path = self.dir / f"usage-{year:04d}-{month:02d}.jsonl"
        path.write_text(json.dumps({"ts": f"{year:04d}-{month:02d}-05T00:00:00+00:00", "ok": True}) + "\n")
        return path

    def test_monthly_file_name(self) -> None:
        os.environ.pop("HUB_SEARCH_LOG", None)
        name = provider._usage_log_path().name
        stamp = datetime.now(timezone.utc).strftime("%Y-%m")
        self.assertEqual(name, f"usage-{stamp}.jsonl")

    def test_prune_drops_whole_old_months_only(self) -> None:
        os.environ["HUB_SEARCH_RETENTION_DAYS"] = "60"
        provider._pruned_on = None
        old = self._touch_month(2020, 1)
        recent = self._touch_month(
            (datetime.now(timezone.utc) - timedelta(days=10)).year,
            (datetime.now(timezone.utc) - timedelta(days=10)).month,
        )
        provider._prune_usage(self.dir)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_retention_zero_disables_pruning(self) -> None:
        os.environ["HUB_SEARCH_RETENTION_DAYS"] = "0"
        provider._pruned_on = None
        old = self._touch_month(2020, 1)
        provider._prune_usage(self.dir)
        self.assertTrue(old.exists())

    def test_unparsable_names_are_left_alone(self) -> None:
        os.environ["HUB_SEARCH_RETENTION_DAYS"] = "60"
        provider._pruned_on = None
        odd = self.dir / "usage-backup.jsonl"
        odd.write_text("")
        provider._prune_usage(self.dir)
        self.assertTrue(odd.exists())


class MultiFileLoadTest(unittest.TestCase):
    def test_merges_rotated_files_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "usage.jsonl").write_text(json.dumps({"ts": "2026-10-31T23:00:00+00:00"}) + "\n")
            (directory / "usage-2026-10.jsonl").write_text(json.dumps({"ts": "2026-10-15T00:00:00+00:00"}) + "\n")
            (directory / "usage-2026-11.jsonl").write_text(json.dumps({"ts": "2026-11-01T00:00:00+00:00"}) + "\n")
            stamps = [row["ts"] for row in load_rows(directory / "usage-2026-11.jsonl")]
            self.assertEqual(stamps, ["2026-11-01T00:00:00+00:00"])

            os.environ["HUB_SEARCH_LOG"] = str(directory / "usage-2026-11.jsonl")
            merged = [row["ts"] for row in load_rows()]
            os.environ.pop("HUB_SEARCH_LOG", None)
            self.assertEqual(merged, sorted(merged))
            self.assertEqual(len(merged), 3)


if __name__ == "__main__":
    unittest.main()
