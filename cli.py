#!/usr/bin/env python3
"""hub-search CLI — usage statistics and raw access to the search log.

    hub-search stats [--days 7] [--json]
    hub-search log [--tail 20] [--json]

The log is written by ``provider.py`` (one JSONL line per search, all profiles) at
``~/.hermes/shared/hub-search/usage.jsonl``; override with ``HUB_SEARCH_LOG``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def log_dir() -> Path:
    override = os.environ.get("HUB_SEARCH_LOG", "").strip()
    if override:
        return Path(override).expanduser().parent
    return Path.home() / ".hermes" / "shared" / "hub-search"


def log_files(directory: Path | None = None) -> list[Path]:
    """Monthly files in chronological order, plus a legacy single-file log if present."""
    directory = directory or log_dir()
    files = sorted(directory.glob("usage-*.jsonl"))
    legacy = directory / "usage.jsonl"
    if legacy.exists():
        files.insert(0, legacy)
    return files


def log_path() -> Path:
    """The file currently being appended to (for messages)."""
    override = os.environ.get("HUB_SEARCH_LOG", "").strip()
    if override:
        return Path(override).expanduser()
    return log_dir() / f"usage-{datetime.now(timezone.utc).strftime('%Y-%m')}.jsonl"


def load_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Read every rotated log (or just ``path`` when given) and return rows oldest first."""
    paths = [path] if path is not None else log_files()
    rows: list[dict[str, Any]] = []
    for one in paths:
        if not one.exists():
            continue
        with one.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    return rows


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(rows: Iterable[dict[str, Any]], days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    """Aggregate usage rows: totals, success rate, latency percentiles, per-day, per-profile, top queries."""
    rows = list(rows)
    now = now or datetime.now(timezone.utc)
    total = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    latencies = [float(r["elapsed"]) for r in rows if isinstance(r.get("elapsed"), (int, float))]
    per_day: Counter[str] = Counter()
    day_ok: Counter[str] = Counter()
    per_profile: Counter[str] = Counter()
    queries: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    cutoff = now - timedelta(days=days)

    for row in rows:
        ts = str(row.get("ts") or "")
        day = ts[:10]
        if day:
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                when = None
            if when is not None and when >= cutoff:
                per_day[day] += 1
                if row.get("ok"):
                    day_ok[day] += 1
        per_profile[str(row.get("profile") or "unknown")] += 1
        if row.get("query"):
            queries[str(row["query"])] += 1
        if not row.get("ok") and row.get("error"):
            errors[str(row["error"])[:80]] += 1

    return {
        "total": total,
        "ok": ok,
        "failed": total - ok,
        "success_rate": (ok / total) if total else 0.0,
        "latency": {
            "p50": _percentile(latencies, 50),
            "p90": _percentile(latencies, 90),
            "max": max(latencies) if latencies else 0.0,
            "mean": (sum(latencies) / len(latencies)) if latencies else 0.0,
        },
        "days": days,
        "per_day": dict(sorted(per_day.items())),
        "day_ok": dict(day_ok),
        "per_profile": dict(per_profile.most_common()),
        "top_queries": queries.most_common(10),
        "top_errors": errors.most_common(5),
    }


def render(summary: dict[str, Any], top: int = 8) -> str:
    total = summary["total"]
    if not total:
        return f"No searches logged yet ({log_path()})."

    latency = summary["latency"]
    lines = [
        f"hub-search: {total} searches — {summary['ok']} ok, {summary['failed']} failed "
        f"({summary['success_rate'] * 100:.1f}% success)",
        f"latency: p50 {latency['p50']:.1f}s  p90 {latency['p90']:.1f}s  "
        f"max {latency['max']:.1f}s  mean {latency['mean']:.1f}s",
    ]

    if summary["per_day"]:
        lines.append("")
        lines.append(f"last {summary['days']} days")
        for day, count in summary["per_day"].items():
            good = summary["day_ok"].get(day, 0)
            bar = "#" * min(40, count)
            lines.append(f"  {day}  {count:>4}  {good / count * 100:>5.1f}%  {bar}")

    if summary["per_profile"]:
        lines.append("")
        lines.append("per profile")
        for profile, count in summary["per_profile"].items():
            lines.append(f"  {profile:<12} {count:>5}")

    if summary["top_queries"]:
        lines.append("")
        lines.append("top queries")
        for query, count in summary["top_queries"][:top]:
            lines.append(f"  {count:>3}x  {query[:64]}")

    if summary["top_errors"]:
        lines.append("")
        lines.append("top errors")
        for error, count in summary["top_errors"]:
            lines.append(f"  {count:>3}x  {error}")

    return "\n".join(lines)


def cmd_stats(args: argparse.Namespace) -> int:
    summary = summarize(load_rows(), days=args.days)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary, top=args.top))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    rows = load_rows()[-args.tail :]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"No searches logged yet ({log_path()}).")
        return 0
    for row in rows:
        status = "ok  " if row.get("ok") else "FAIL"
        stamp = str(row.get("ts") or "")[:19].replace("T", " ")
        elapsed = float(row.get("elapsed") or 0.0)
        profile = str(row.get("profile") or "?").ljust(10)
        query = str(row.get("query") or "")[:60]
        print(f"{stamp}  {status}  {elapsed:>5.1f}s  {profile}  {query}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-search", description="Hub-subscription web search usage statistics")
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="aggregate usage statistics")
    stats.add_argument("--days", type=int, default=7)
    stats.add_argument("--top", type=int, default=8)
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    log = sub.add_parser("log", help="show recent searches")
    log.add_argument("--tail", type=int, default=20)
    log.add_argument("--json", action="store_true")
    log.set_defaults(func=cmd_log)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
