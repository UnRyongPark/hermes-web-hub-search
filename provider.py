"""``hub-search`` — web search on the OpenAI subscription via the OpenCodex hub.

The hub exposes OpenAI's hosted ``web_search`` tool, so searches executed here run
upstream on the subscription account. Unlike the ``openai-native`` backend (which only
declares the hosted tool and therefore cannot run client-side), this provider has a real
``search()`` implementation, so the same code path serves both the model's ``web_search``
tool call and programmatic callers (``execute_code``, subagents, cron).

Env (optional):
  HUB_SEARCH_BASE   default http://192.168.1.116:10100
  HUB_SEARCH_MODEL  default combo/ds41f
  HUB_SEARCH_TIMEOUT default 50 (seconds, soft deadline)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://192.168.1.116:10100"
_DEFAULT_MODEL = "combo/ds41f"
_DEFAULT_TIMEOUT = 50.0

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
        key = _api_key()
        if not key:
            return _search_fail("OPENCODEX_HERMES_GPT_API_KEY is not set for this profile")
        base = os.environ.get("HUB_SEARCH_BASE", _DEFAULT_BASE).rstrip("/")
        model = os.environ.get("HUB_SEARCH_MODEL", _DEFAULT_MODEL)
        timeout = float(os.environ.get("HUB_SEARCH_TIMEOUT", _DEFAULT_TIMEOUT))
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
