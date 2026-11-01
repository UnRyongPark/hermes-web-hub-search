# hermes-web-hub-search

Hermes Agent web-search provider that runs every search **on the OpenAI subscription**
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

Restart the gateway for the change to take effect.

## Configuration

| env | default | meaning |
| --- | --- | --- |
| `HUB_SEARCH_BASE` | `http://192.168.1.116:10100` | OpenCodex hub origin |
| `HUB_SEARCH_MODEL` | `combo/ds41f` | model that carries the hosted tool call |
| `HUB_SEARCH_TIMEOUT` | `50` | soft deadline in seconds |

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
