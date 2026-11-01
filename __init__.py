"""Register the hub-subscription web search provider and its usage commands."""

from __future__ import annotations

from .provider import HubSearchProvider


def _parse_int(raw: str, default: int) -> int:
    """Pull an int out of ``"30"``, ``"--days 30"`` or ``"-n 30"`` style arguments."""
    tokens = [t for t in raw.split() if t not in ("--days", "--tail", "-n", "-d")]
    for token in tokens:
        try:
            return max(1, int(token))
        except ValueError:
            continue
    return default


def _stats_command(raw_args: str) -> str:
    """``/hubsearchstats [days]`` — totals, latency, per-day and per-profile breakdown."""
    from .cli import load_rows, render, summarize

    days = _parse_int(raw_args, 7)
    lines = render(summarize(load_rows(), days=days))
    return lines if days == 7 else f"{lines}\n\n(window: last {days} days; totals cover the whole log)"


def _log_command(raw_args: str) -> str:
    """``/hubsearchlog [n]`` — the most recent n searches, one line each."""
    from .cli import load_rows

    tail = _parse_int(raw_args, 10)
    rows = load_rows()[-tail:]
    if not rows:
        return "No searches logged yet."
    out = []
    for row in rows:
        stamp = str(row.get("ts") or "")[:16].replace("T", " ")
        status = "ok  " if row.get("ok") else "FAIL"
        elapsed = float(row.get("elapsed") or 0.0)
        profile = str(row.get("profile") or "?").ljust(9)
        query = str(row.get("query") or "")[:50]
        out.append(f"{stamp} {status} {elapsed:>5.1f}s {profile} {query}")
    return "\n".join(out)


def register(ctx) -> None:
    """Register the hub search provider plus ``/hubsearchstats`` and ``/hubsearchlog``."""
    ctx.register_web_search_provider(HubSearchProvider())
    ctx.register_command(
        "hubsearchstats",
        _stats_command,
        description="Hub search usage statistics (optionally: /hubsearchstats 30)",
        args_hint="[days]",
    )
    ctx.register_command(
        "hubsearchlog",
        _log_command,
        description="Recent hub searches (optionally: /hubsearchlog 20)",
        args_hint="[n]",
    )
