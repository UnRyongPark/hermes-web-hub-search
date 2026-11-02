# Changelog

## 1.2.0

- One request now returns the whole retrieved set. The instructions ask the carrier for every
  distinct hit the hosted tool produced (up to `HUB_SEARCH_MAX_RESULTS`, default 15) instead of
  the citation subset it would otherwise pick, which had capped every search at 5 rows no matter
  what the caller asked for. Measured 10/10 and 5/5 filled in a single round trip.
- Default carrier moves to `opencode-go/gpt-6-luna`: the same model on the hub's `openai` lane took
  20-27 s for ten results that this lane returned in 11-15 s.
- Knobs are dotenv-first and the `load_env()` memo is dropped per search. The gateway copies `.env`
  into `os.environ` at boot, where it outranks the file, so edits used to need a gateway restart.
- Usage log records `model`, so a lane change is visible in the log instead of inferred from timing.

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
