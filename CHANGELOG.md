# Changelog

## 1.1.0

- Carry the hosted search on a native GPT route. `combo/*` aliases are translated, so the hub
  fell back to its sidecar loop (helper model rewrites the query, one search per variant):
  3 searches / 2 iterations / 119 s in the hub log, sources arriving at 124 s. Measured end to
  end through the provider with `gpt-6-luna` as carrier: 12-13 s.
- Default soft deadline 50 s -> 90 s, sized to bound a hung stream instead of cutting healthy
  work; `HUB_SEARCH_TIMEOUT`, `HUB_SEARCH_MODEL` and `HUB_SEARCH_BASE` are read through the
  config layer so a change lands without restarting the gateway.
- Document the latency/route coupling and the source-count trade-off (2-5 sources on the native
  route, up to 20 on the sidecar loop) in the README.

## 1.0.0

- Initial provider: hosted `web_search` through the OpenCodex hub, soft deadline and usage log.
