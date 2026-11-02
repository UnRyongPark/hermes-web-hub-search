"""``hub-search`` — web search on the OpenAI subscription via the OpenCodex hub.

The hub exposes OpenAI's hosted ``web_search`` tool, so searches executed here run
upstream on the subscription account. Unlike the ``openai-native`` backend (which only
declares the hosted tool and therefore cannot run client-side), this provider has a real
``search()`` implementation, so the same code path serves both the model's ``web_search``
tool call and programmatic callers (``execute_code``, subagents, cron).

The carrier model must sit on a *native passthrough* route (a bare ``gpt-*`` id). A
translated route such as ``combo/*`` makes the hub emulate the hosted tool with a sidecar
loop — one helper-model call plus one search per rewritten query — which measured 119-150 s
against 13-19 s here (see README).

One call returns the whole retrieved set, not a cited subset: the instructions ask for every
distinct result the hosted tool returned (up to HUB_SEARCH_MAX_RESULTS), which is what fills
the caller's ``limit`` in a single round trip.

Env (optional):
  HUB_SEARCH_BASE         default http://192.168.1.116:10100
  HUB_SEARCH_MODEL        default gpt-6-luna (cheapest carrier measured at the hub)
  HUB_SEARCH_TIMEOUT      default 90 (seconds, soft deadline)
  HUB_SEARCH_MAX_RESULTS  default 20 (cap on results requested from the carrier)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

_pruned_on: date | None = None

_DEFAULT_BASE = "http://192.168.1.116:10100"
_DEFAULT_MODEL = "gpt-6-luna"
_DEFAULT_TIMEOUT = 90.0
_DEFAULT_MAX_RESULTS = 15


def _instructions(wanted: int) -> str:
    """Ask for the whole result set, not the model's citation subset.

    Requesting a cited subset returned 5 rows no matter what the caller asked for; asking for
    every distinct hit the tool returned filled the same call with 15. The model is told not to
    filter, summarise or answer — this is a retrieval turn, and the caller does the reading.
    """
    return (
        "You are a web search backend. Run the web search tool for the query. Then output ONLY a JSON "
        'array of objects {"title", "url", "snippet"}, one per distinct result the tool returned, in '
        f"the order returned, up to {wanted}. Do not filter for relevance, do not summarise, do not "
        "answer the query, no prose, no markdown fences, no trailing commentary. Never invent URLs."
    )


def _profile_env_files() -> list[Path]:
    files: list[Path] = []
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        files.append(Path.home() / ".hermes" / "profiles" / profile / ".env")
    files.append(Path.home() / ".hermes" / ".env")
    return files


def _key_env_candidates() -> list[str]:
    """Env vars to try, most specific first.

    Every profile names its hub key differently (``OPENCODEX_HERMES_GPT_API_KEY``,
    ``OPENCODEX_BEAVER_API_KEY``, ...), so the profile's own provider block — the one
    pointing at the hub origin — is consulted for its ``key_env``.
    """
    candidates = ["HUB_SEARCH_KEY", "OPENCODEX_HERMES_GPT_API_KEY"]
    try:
        from hermes_cli.config import load_config

        host = urlparse(os.environ.get("HUB_SEARCH_BASE", _DEFAULT_BASE)).netloc
        providers = (load_config() or {}).get("providers") or {}
        for block in providers.values():
            if not isinstance(block, dict):
                continue
            api = str(block.get("api") or "")
            key_env = str(block.get("key_env") or "")
            if key_env and host and urlparse(api).netloc == host and key_env not in candidates:
                candidates.insert(0, key_env)
    except Exception as exc:  # config layer unavailable (stripped install, tests)
        logger.debug("hub-search config lookup failed: %s", exc)
    return candidates


def _api_key() -> str:
    for var in _key_env_candidates():
        value = get_provider_env(var)
        if value:
            return value
        for path in _profile_env_files():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    match = re.match(rf"^{re.escape(var)}=(.*)$", line.strip())
                    if match:
                        found = match.group(1).strip().strip('"').strip("'")
                        if found:
                            return found
            except OSError:
                continue
    return ""


def _clean_url(url: str) -> str:
    url = re.sub(r"[?&]utm_(source|medium|campaign)=[^&]*", "", url)
    return url.replace("?&", "?").rstrip("?&")


def _parse_array(text: str) -> list[dict[str, str]]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(text[start : end + 1])
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        out.append(
            {
                "url": url,
                "title": str(row.get("title") or "").strip(),
                "snippet": str(row.get("snippet") or row.get("description") or "").strip(),
            }
        )
    return out


_MD_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _salvage_rows(text: str) -> list[dict[str, str]]:
    """Pull every URL out of a prose answer.

    The carrier is a chat model, so now and then it answers the question instead of emitting the
    JSON array — measured as 2 of 27 searches, each a fast 5-6 s reply that the parser scored as
    zero results. A cited answer still carries real sources, so recovering them turns a hard
    failure into a thin but honest result set.
    """
    text = text.strip()
    if not text:
        return []
    spans: list[tuple[int, int, str, str]] = []
    for match in _MD_LINK_RE.finditer(text):
        spans.append((match.start(), match.end(), match.group(1).strip(), match.group(2)))
    linked = [(start, end) for start, end, _, _ in spans]
    for match in _BARE_URL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in linked):
            continue
        spans.append((match.start(), match.end(), "", match.group(0).rstrip(".,;:!?")))
    spans.sort(key=lambda item: item[0])

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for start, end, title, url in spans:
        url = _clean_url(url)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        # Window around the link rather than a sentence: a cited answer often puts the link in
        # trailing parens, so cutting at the sentence end inside the URL left "()" as the snippet.
        window = text[max(0, start - 240) : end + 40]
        snippet = re.sub(r"\[[^\]]*\]\([^)]*\)", "", window)
        snippet = _BARE_URL_RE.sub("", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip(" .-—()[]")
        out.append(
            {
                "url": url,
                "title": title or re.sub(r"^https?://", "", url).split("/")[0],
                "snippet": snippet[:240],
            }
        )
    return out


def _search_fail(error: str) -> dict[str, Any]:
    return {"success": False, "error": error}


def _usage_log_path() -> Path:
    """Monthly log file, rotated by month and pruned by age (see ``_prune_usage``)."""
    override = os.environ.get("HUB_SEARCH_LOG", "").strip()
    if override:
        return Path(override).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m")
    return _usage_dir() / f"usage-{stamp}.jsonl"


def _usage_dir() -> Path:
    override = os.environ.get("HUB_SEARCH_LOG", "").strip()
    if override:
        return Path(override).expanduser().parent
    return Path.home() / ".hermes" / "shared" / "hub-search"


def _prune_usage(directory: Path) -> None:
    """Delete rotated logs older than HUB_SEARCH_RETENTION_DAYS (default 180; 0 disables).

    Runs at most once per process per day so the append path stays cheap.
    """
    global _pruned_on
    today = datetime.now(timezone.utc).date()
    if _pruned_on == today:
        return
    _pruned_on = today
    try:
        keep_days = int(os.environ.get("HUB_SEARCH_RETENTION_DAYS", "180"))
    except ValueError:
        keep_days = 180
    if keep_days <= 0:
        return
    cutoff = today - timedelta(days=keep_days)
    for old in directory.glob("usage-*.jsonl"):
        try:
            stamp = datetime.strptime(old.stem.split("-", 1)[1], "%Y-%m").date()
        except (IndexError, ValueError):
            continue
        # Keep the whole month that contains the cutoff; drop months that end before it.
        month_end = (stamp.replace(day=28) + timedelta(days=5)).replace(day=1) - timedelta(days=1)
        if month_end < cutoff:
            try:
                old.unlink()
            except OSError:
                pass


def _profile_name() -> str:
    try:
        from hermes_cli.config import get_config_path

        return get_config_path().parent.name or "default"
    except Exception:
        return os.environ.get("HERMES_PROFILE", "").strip() or "default"


def _log_usage(query: str, limit: int, result: dict[str, Any], elapsed: float, model: str = "") -> None:
    """Append one JSONL line per search for ``hub-search stats`` / dashboards.

    Best effort: analytics must never break a search.
    """
    ok = bool(result.get("success"))
    hits = len(((result.get("data") or {}).get("web") or [])) if ok else 0
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": _profile_name(),
        "query": query[:300],
        # Hermes asks for a bucketed fetch count (e.g. a request for 2 arrives as 10) and
        # slices the caller's share afterwards, so this is the fetch size, not the ask.
        "fetch_limit": int(limit),
        "model": model,
        "ok": ok,
        "results": hits,
        "elapsed": round(elapsed, 2),
    }
    if not ok:
        entry["error"] = str(result.get("error") or "")[:200]
    try:
        path = _usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_usage(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("hub-search usage log failed: %s", exc)


class HubSearchProvider(WebSearchProvider):
    """Search through the hub's hosted OpenAI ``web_search`` tool."""

    NAME = "hub-search"
    DISPLAY_NAME = "Hub (OpenAI subscription)"
    KEY_ENV = "OPENCODEX_HERMES_GPT_API_KEY"
    HOSTED = True

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    def is_available(self) -> bool:
        return bool(_api_key())

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        started = time.time()
        try:
            result = self._run_search(query, limit)
        except Exception as exc:  # never let a provider bug escape into the tool layer
            logger.warning("hub-search crashed: %s", exc)
            result = _search_fail(f"hub search crashed: {exc}")
        model = str(result.pop("_model", "") or "")
        _log_usage(query, limit, result, time.time() - started, model)
        return result

    def _run_search(self, query: str, limit: int = 5) -> dict[str, Any]:
        key = _api_key()
        if not key:
            return _search_fail("OPENCODEX_HERMES_GPT_API_KEY is not set for this profile")
        # load_env() memoises the .env file per process, so a long-lived gateway would keep
        # serving the values it booted with. Drop the cache first: retuning the carrier or the
        # deadline then lands on the next search instead of on the next restart.
        def setting(name: str) -> str:
            """Read a knob so the profile's .env wins, every call.

            Two caches stand between this provider and a retuned knob: ``load_env()`` memoises
            the parsed file, and the gateway copies .env into ``os.environ`` at boot, where it
            outranks the file for ``get_provider_env``. Dropping the memo and reading
            dotenv-first means editing .env takes effect on the next search rather than on the
            next gateway restart.
            """
            try:
                from hermes_cli.config import get_env_value_prefer_dotenv, invalidate_env_cache

                invalidate_env_cache()
                return (get_env_value_prefer_dotenv(name) or "").strip()
            except Exception as exc:  # stripped install / early import
                logger.debug("hub-search dotenv-first lookup unavailable: %s", exc)
                return (get_provider_env(name) or "").strip()

        base = (setting("HUB_SEARCH_BASE") or _DEFAULT_BASE).rstrip("/")
        model = setting("HUB_SEARCH_MODEL") or _DEFAULT_MODEL
        try:
            timeout = float(setting("HUB_SEARCH_TIMEOUT") or _DEFAULT_TIMEOUT)
        except ValueError:
            timeout = _DEFAULT_TIMEOUT
        limit = max(1, min(int(limit or 5), 20))
        try:
            fetch = min(max(1, int(get_provider_env("HUB_SEARCH_MAX_RESULTS") or _DEFAULT_MAX_RESULTS)), 25)
        except ValueError:
            fetch = _DEFAULT_MAX_RESULTS
        # Fill the caller's ask rather than over-fetching far past it: every extra row costs
        # output tokens, and output tokens are what the carrier spends its time generating.
        fetch = min(fetch, max(limit, 8))

        body = {
            "model": model,
            "stream": True,
            "instructions": _instructions(fetch),
            "input": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": max(1200, min(4200, 400 + 180 * fetch)),
        }
        request = urllib.request.Request(
            f"{base}/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )

        text: list[str] = []
        sources: list[dict[str, str]] = []
        deadline = time.time() + timeout
        try:
            with urllib.request.urlopen(request, timeout=timeout + 10) as response:
                for raw in response:
                    if time.time() > deadline:
                        logger.warning("hub-search soft deadline hit; returning %d sources", len(sources))
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except Exception:
                        continue
                    etype = event.get("type")
                    if etype == "response.output_text.delta":
                        text.append(event.get("delta") or "")
                    elif etype == "response.output_item.done":
                        item = event.get("item") or {}
                        if item.get("type") == "web_search_call" and item.get("sources"):
                            sources = [
                                {"url": _clean_url(s.get("url", "")), "title": s.get("title", ""), "snippet": ""}
                                for s in item["sources"]
                                if str(s.get("url", "")).startswith("http")
                            ]
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            logger.warning("hub-search HTTP %s: %s", exc.code, detail)
            return _search_fail(f"hub HTTP {exc.code}: {detail}")
        except Exception as exc:
            logger.warning("hub-search failed: %s", exc)
            return _search_fail(f"hub search failed: {exc}")

        raw_text = "".join(text)
        rows = _parse_array(raw_text)
        mode = "json"
        if not rows:
            rows = _salvage_rows(raw_text)
            mode = "salvage" if rows else "none"
        if not rows:
            rows = sources
        else:
            seen = {r["url"] for r in rows}
            rows += [s for s in sources if s["url"] not in seen]
        logger.info(
            "hub-search '%s': text=%d chars parsed=%d sources=%d mode=%s",
            query,
            len(raw_text),
            len(rows),
            len(sources),
            mode,
        )

        hits: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row in rows:
            url = _clean_url(row["url"])
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            hits.append(
                {"title": row["title"], "url": url, "description": row["snippet"], "position": len(hits) + 1}
            )
            if len(hits) >= limit:
                break
        if not hits:
            # Say which failure it was. "No results" covered three different things — a stalled
            # upstream, a prose reply with no links, and a genuinely empty result set — and only
            # the last one means the query found nothing.
            if not raw_text.strip():
                return _search_fail(f"hub produced no output within {timeout:.0f}s (upstream stall)")
            return _search_fail("hub replied without any URLs")
        return {"success": True, "data": {"web": hits}, "_model": model}
