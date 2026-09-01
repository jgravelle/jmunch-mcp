# jmunch-mcp

<!-- mcp-name: io.github.jgravelle/jmunch-mcp -->

Transparent MCP proxy that reduces the token cost of large upstream tool responses for nearly every other MCP server imaginable. Wraps a single upstream MCP, forwards every call, and handle-ifies fat payloads into content-aware backends the agent can query with a small set of universal verbs (`peek`, `slice`, `search`, `aggregate`, `describe`, `list_handles`).

## Benchmarks

Measured end-to-end against two popular real-world MCP servers. Each run fires a fixed script of tool calls twice — once direct, once through jmunch-mcp — with three follow-up `jmunch.*` verb calls on the proxied side to model an agent drilling into a large result rather than slurping it whole.

| suite | upstream | direct tokens | via jmunch-mcp | saved |
|---|---|---:|---:|---:|
| GitHub (`facebook/react` issues/PRs/commits) | `@modelcontextprotocol/server-github` | 379,878 | 44,328 | **335,550 (88.3%)** |
| Firecrawl (Wikipedia scrapes + site map + search) | `firecrawl-mcp` | 259,574 | 2,928 | **256,646 (98.9%)** |

Wall-clock time was also faster with the proxy on both suites, despite the extra verb calls — the agent never has to page through the fat payload:

| suite | direct | via jmunch-mcp | delta |
|---|---:|---:|---:|
| GitHub    |  8.4s |  6.8s | **−1.6s (−19.0%)** |
| Firecrawl | 16.4s |  9.2s | **−7.2s (−43.9%)** |

Tabular content (GitHub) routes to the SQLite backend and answers `peek`/`slice`/`aggregate`; JSON content (Firecrawl scrape/map) routes to the JSON-tree backend and answers `peek`/`slice` (JSONPath)/`search`. See [bench/README.md](bench/README.md) to reproduce.

## Install

```bash
pip install jmunch-mcp
```

From source:

```bash
git clone https://github.com/jgravelle/jmunch-mcp
cd jmunch-mcp
pip install -e .
```

## Quickstart

```bash
jmunch-mcp init
```

`init` scans three sources — your MCP client configs (Claude Desktop, Claude Code, Cursor, Windsurf, Continue), running processes, and a small catalog of popular upstreams (GitHub, Firecrawl, filesystem, fetch, Brave Search, Slack) — and renders a checklist. Tick the upstreams you want wrapped, and it writes one `<name>.toml` per selection into `./configs/`. Non-interactive flags: `--yes` (pick everything already registered in a client), `--dry-run`, `--overwrite`, `--out <dir>`, `--no-running`, `--no-catalog`.

### Manual

```bash
jmunch-mcp --config examples/config.toml
```

Configure your MCP client to launch `jmunch-mcp --config <path>` instead of the upstream server directly. Add `--report` to print a session summary on shutdown.

## Gateway mode (v2 — universal proxy)

The MCP proxy above saves tokens for MCP clients. The **gateway** saves tokens for *any* AI application that speaks the OpenAI or Anthropic HTTP API — LangChain, LlamaIndex, CrewAI, AutoGen, Continue, Cline, Aider, or a raw SDK. No code changes in the app; just point `base_url` at jmunch.

```bash
pip install 'jmunch-mcp[gateway]'
jmunch-mcp gateway --config configs/gateway.example.toml
# listening on http://127.0.0.1:7879
```

Point your app:

```bash
# OpenAI SDK, LangChain, Aider, Continue, Cline, Ollama-compat apps:
export OPENAI_API_BASE=http://127.0.0.1:7879/v1

# Native Anthropic SDK / Claude Code:
export ANTHROPIC_BASE_URL=http://127.0.0.1:7879
```

What it does, transparently:

- **Handle-ifies fat tool_results** in outgoing requests — your app's tool returns 100KB of JSON, the model sees a 1KB summary + opaque handle.
- **Injects jmunch verbs** (`peek`, `slice`, `search`, `aggregate`, `describe`, `summarize`, `list_handles`) into the request's `tools` array so the model can drill in.
- **Short-circuits verb calls** — when the model calls `jmunch_peek`, the gateway resolves it locally against the handle registry and synthesizes the follow-up turn. The app never sees jmunch tool_calls; those completions cost zero upstream tokens.
- **Persists handles** to `~/.jmunch/handles.db` with a configurable TTL so they survive restarts and cross-session reads.
- **Streams both ways** — OpenAI SSE and Anthropic event streams are buffer-then-replayed with correct verb resolution.

Per-request controls via headers:

- `X-Jmunch-Upstream: <name>` — override the configured upstream.
- `X-Jmunch-Inject: false` — disable tool injection for this call (pure pass-through + request-side handle-ify only).

Metrics flow into the same dashboard as the MCP proxy. Filter with `?surface=gateway` or `?surface=mcp` on `/api/stats` and `/api/calls`.

## Dashboard

A read-only local web UI over the metrics DB each proxy writes to. Shows cumulative totals, per-upstream breakdowns, and a time series of forwarded calls.

```bash
jmunch-mcp dashboard              # http://127.0.0.1:7878
jmunch-mcp dashboard --open       # also open in your default browser
```

Flags: `--port` (default `7878`), `--host` (default `127.0.0.1`), `--db <path>` to point at a non-default metrics DB, `--open` to launch the browser. Metrics only populate once proxies have recorded calls, so run your client against a wrapped upstream first.

## License

jmunch-mcp is released under the [MIT License](LICENSE) — free to use, modify, distribute, and embed in commercial products.

Note that licensing of **upstream MCP services** you proxy through jmunch-mcp is governed by those services' own terms. This applies to third-party MCP servers (GitHub, filesystem providers, vendor APIs) and to any sibling tools in the broader retrieval ecosystem you may compose with — check each upstream's license before redistribution.
