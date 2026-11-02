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

In any Hermes chat (no model turn, no terminal needed):

```
/hubsearch_stats        # 7-day window
/hubsearch_stats 30     # 30-day window
/hubsearch_log 20       # last 20 searches
```

Register plugin commands **hyphenated** (`hubsearch-stats`). Telegram accepts only `[a-z0-9_]`,
so the menu sanitizer renders them as `/hubsearch_stats`, and the gateway turns underscores
back into hyphens before resolving a plugin command. Registering the underscored name instead
breaks that round trip: the lookup asks for `hubsearch-stats`, finds nothing, and answers
"Unknown command /hubsearch_stats" — even after a restart, because the name itself is wrong.

The 60-slot BotCommand menu is usually already full and plugin commands sort last (they get
cut), so name them explicitly (priority matching uses the sanitized, underscored form):

```bash
hermes config set platforms.telegram.extra.command_menu.priority '["hubsearch_stats","hubsearch_log"]'
```

`set_my_commands` is pushed once at adapter connect, so after enabling the plugin on a
running gateway push the menu (or restart). `scripts/push_menu.py` does that and reads
the result back with `get_my_commands`.

Or from a shell:

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
| `HUB_SEARCH_MODEL` | `gpt-6-luna` | model that carries the hosted tool call — must be a native passthrough (see Trade-offs) |
| `HUB_SEARCH_TIMEOUT` | `90` | soft deadline in seconds, measured from the request |
| `HUB_SEARCH_LOG` | `~/.hermes/shared/hub-search/usage.jsonl` | usage log path |
| `HUB_SEARCH_KEY` | _(profile provider block)_ | explicit hub key override |

Credentials: `OPENCODEX_HERMES_GPT_API_KEY` from the environment, the active profile's
`.env`, or `~/.hermes/.env`.

## Trade-offs

- **Latency depends entirely on the carrier model's route and lane, not on the query.** The default
  `HUB_SEARCH_MODEL` must therefore be a model the hub serves on a *native passthrough* route
  (`gpt-*`), never a `combo/*` alias.
  - `combo/ds41f` is translated, so the hub cannot let upstream run the hosted tool. It runs an
    agentic **sidecar loop** instead: a helper model (`webSearchSidecar.model`, here
    `gpt-5.6-luna`) rewrites the query into variants, runs the hosted search once per variant,
    formats the hits, and re-POSTs to the routed model. Hub log for one such turn:
    `[web-search-loop] done — 3 searches, 2 iterations, 119367ms`. Observed range 35–150 s,
    sources only materialising at 124 s. A 50 s ceiling cut that loop and reported
    `hub returned no results` — the hub was healthy the whole time.
  - A `gpt-*` carrier keeps the hosted tool native: upstream executes the search inside the same
    turn. Measured end to end at 10–13 s, with the search itself finishing in 5–7 s.
  - Trade-off: the native route returns fewer sources per query (2–5 observed) than the sidecar
    loop (up to 20), at roughly a tenth of the wall time.
- `HUB_SEARCH_TIMEOUT` must cover source arrival, not the answer. 90 s is ample for a native
  carrier and still bounds a hung stream.
- Hermes' tool-level memo cache serves repeats instantly.
- **Breadth comes from the ask, not from a second pass.** Instructing the carrier to return the
  cited subset yielded 5 rows however many the caller wanted; instructing it to return every
  distinct hit the tool produced filled the same single call with 10-15. So one request covers
  both the fast path and the wide path: 10-15 s for 10 results, 10-13 s for 5.
- **Lane is a billing decision before it is a speed decision.** The default stays on the hub's
  `openai` provider: that lane draws on the ChatGPT subscription, which is the whole point of this
  provider. It costs 20-35 s for ten results. The `opencode-go` (Zen) lane served the same ten
  results in 11-15 s, but pays per call on a separate plan, so it is opt-in only — set
  `HUB_SEARCH_MODEL=opencode-go/gpt-6-luna` and accept the separate bill. Re-time both lanes
  whenever either changes; never switch lanes for latency alone.
- **Env knobs are dotenv-first.** The gateway copies `.env` into `os.environ` at boot, where it
  outranks the file, and `load_env()` memoises the parse. The provider drops that memo and reads
  dotenv-first so retuning takes effect on the next search, not the next gateway restart.
- **Snippets**: when the soft deadline fires, only source titles/URLs are returned.
- **Cost**: a search is a full model round trip on the hub, so it is billed like any
  other call. With `openai-native` the same search was inlined into the answering turn.

## License

MIT
