# hermes-web-hub-search

**When you put OpenCodex behind Hermes, this plugin hands Hermes' search to Codex.**

It is a Hermes Agent web-search provider: every search runs **on the OpenAI subscription**
through an OpenCodex hub, instead of declaring OpenAI's hosted tool and leaving the
client-side path broken.

## Why

`web.search_backend: openai-native` only *declares* `{"type": "web_search"}` on the wire:
the hub executes the search server-side, so a normal model turn works. But that backend has
no client-side `search()` implementation, which means programmatic callers —
`execute_code` → `hermes_tools.web_search`, subagents, cron — always fail and then silently
degrade to a keyless free ring.

This provider implements a real `search()` that calls the hub with the same hosted tool.
One code path serves both the model's `web_search` call and programmatic callers, and the
search still runs upstream on the subscription account.

## How it works

1. `POST {HUB_BASE}/v1/responses` with `tools: [{"type": "web_search"}]` and a JSON-array
   output contract.
2. Reads the SSE stream: `web_search_call` items carry the upstream sources;
   `response.output_text.delta` carries the (snippet-bearing) JSON array.
3. If the model has not finished within the soft deadline, the source list is returned
   without snippets rather than failing the call.
4. Results are mapped to Hermes' `{"success": true, "data": {"web": [...]}}` shape.

## Install

```bash
git clone <this-repo> ~/repos/hermes-web-hub-search
for p in family tech po marketing operator researcher; do
  ln -sfn ~/repos/hermes-web-hub-search ~/.hermes/profiles/$p/plugins/web/hub-search
done
hermes config set web.search_backend hub-search
```

Reload the running gateway's plugins instead of restarting it:

```bash
hermes plugins disable web/hub-search && hermes plugins enable web/hub-search
```

## Usage statistics

Every search appends one JSONL line (query, profile, ok, result count, seconds, error) to
`~/.hermes/shared/hub-search/usage-YYYY-MM.jsonl` — monthly files, shared across profiles,
skipped silently when the log cannot be written. Months that end before
`HUB_SEARCH_RETENTION_DAYS` (default 180, `0` disables) are pruned once a day on write.

`fetch_limit` is the **fetch** count Hermes asks the provider for, not the number the caller
requested: Hermes buckets the ask (2 → 10) so near-identical limits share one cached search,
then slices the caller's share afterwards. `results` is what the provider actually returned.

The web dashboard and log viewer are not built yet — `stats --json` is their intended input.

```bash
ln -sfn ~/repos/hermes-web-hub-search/cli.py ~/.local/bin/hub-search
hub-search stats              # totals, success rate, p50/p90 latency, per-day bars, top queries
hub-search stats --days 30    # wider window (totals always cover the whole log)
hub-search stats --json       # machine-readable, for a dashboard
hub-search log --tail 20      # raw recent searches, one line each
```

```
hub-search: 4 searches — 3 ok, 1 failed (75.0% success)
latency: p50 12.3s  p90 34.4s  max 34.4s  mean 19.0s

last 7 days
  2026-11-01     4   75.0%  ####
```

Roadmap: a small web dashboard and a log viewer over the same JSONL.

## Configuration

| env | default | meaning |
| --- | --- | --- |
| `HUB_SEARCH_BASE` | `http://192.168.1.116:10100` | OpenCodex hub origin |
| `HUB_SEARCH_MODEL` | `combo/ds41f` | model that carries the hosted tool call |
| `HUB_SEARCH_TIMEOUT` | `50` | soft deadline in seconds |
| `HUB_SEARCH_LOG` | `~/.hermes/shared/hub-search/usage.jsonl` | usage log path |
| `HUB_SEARCH_KEY` | _(profile provider block)_ | explicit hub key override |

Credentials: `OPENCODEX_HERMES_GPT_API_KEY` from the environment, the active profile's
`.env`, or `~/.hermes/.env`.

## Trade-offs

- **Latency**: the hosted search itself takes 12–50 s upstream (it varies). Hermes'
  tool-level memo cache serves repeats instantly; set `HUB_SEARCH_TIMEOUT` if a hard
  ceiling is needed.
- **Snippets**: when the soft deadline fires, only source titles/URLs are returned.
- **Cost**: a search is a full model round trip on the hub, so it is billed like any
  other call. With `openai-native` the same search was inlined into the answering turn.

## License

MIT
