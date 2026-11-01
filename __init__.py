"""Register the hub-subscription web search provider."""

from __future__ import annotations

from .provider import HubSearchProvider


def register(ctx) -> None:
    """Register the hub search provider with Hermes."""
    ctx.register_web_search_provider(HubSearchProvider())
