"""``hub-search`` — web search on the OpenAI subscription via the OpenCodex hub.

The hub exposes OpenAI's hosted ``web_search`` tool, so searches executed here run
upstream on the subscription account. Unlike the ``openai-native`` backend (which only
declares the hosted tool and therefore cannot run client-side), this provider has a real
``search()`` implementation, so the same code path serves both the model's ``web_search``
tool call and programmatic callers (``execute_code``, subagents, cron).

Env (optional):
  HUB_SEARCH_BASE   default http://192.168.1.116:10100
  HUB_SEARCH_MODEL  default combo/ds41f
  HUB_SEARCH_TIMEOUT default 180 (seconds, soft deadline; hub sidecar searches
                    legitimately take 35-150s, see README)
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
_DEFAULT_MODEL = "combo/ds41f"
_DEFAULT_TIMEOUT = 180.0

_INSTRUCTIONS = (
    "You are a web search backend. Use the web search tool for the query, then reply with ONLY a "
    'JSON array of objects {"title", "url", "snippet"} — snippet at most 20 words, taken from the '
    "source. No prose, no markdown fences, no trailing commentary. Never invent URLs."
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


def _log_usage(query: str, limit: int, result: dict[str, Any], elapsed: float) -> None:
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
        _log_usage(query, limit, result, time.time() - started)
        return result

    def _run_search(self, query: str, limit: int = 5) -> dict[str, Any]:
        key = _api_key()
        if not key:
            return _search_fail("OPENCODEX_HERMES_GPT_API_KEY is not set for this profile")
        # get_provider_env reads the profile .env through the config layer on every call, so
        # a timeout change lands in a running gateway without restarting it.
        base = (get_provider_env("HUB_SEARCH_BASE") or _DEFAULT_BASE).rstrip("/")
        model = get_provider_env("HUB_SEARCH_MODEL") or _DEFAULT_MODEL
        try:
            timeout = float(get_provider_env("HUB_SEARCH_TIMEOUT") or _DEFAULT_TIMEOUT)
        except ValueError:
            timeout = _DEFAULT_TIMEOUT
        limit = max(1, min(int(limit or 5), 20))

        body = {
            "model": model,
            "stream": True,
            "instructions": _INSTRUCTIONS,
            "input": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": max(600, min(2600, 400 + 420 * limit)),
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
        if not rows:
            rows = sources
        else:
            seen = {r["url"] for r in rows}
            rows += [s for s in sources if s["url"] not in seen]
        logger.info(
            "hub-search '%s': text=%d chars parsed=%d sources=%d", query, len(raw_text), len(rows), len(sources)
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
            return _search_fail("hub returned no results")
        return {"success": True, "data": {"web": hits}}
