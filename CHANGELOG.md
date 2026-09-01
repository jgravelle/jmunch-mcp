# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `[tool.hatch.build.targets.sdist]` allowlist. The sdist had no target block, so
  contents fell back to hatchling's default selection — which honors
  `.gitignore`, making `.gitignore` the only thing between a working-tree file
  and PyPI. An allowlist inverts the default: a path is out unless named.
  Verified against planted decoys (`.env`, `*.bak-*`, `*.local.toml`).
- `tests/test_no_inline_credentials.py` fails the suite on a secret in a tracked
  config, and asserts the gitignored `configs/*.local.toml` escape hatch is
  really ignored.
- `server.json` for the MCP registry, and `tests/test_version_sync.py` to gate
  it against `pyproject.toml` — including `packages[].version`, which the
  registry will happily accept moving on its own. The same file pins the
  registry's own schema constraints: the first publish attempt was rejected
  422 on a 141-character description against a 100-character limit, and the
  publish is human-typed with a five-minute token, so a rejection there costs a
  full round trip.
- `CLAUDE.md` and `RELEASING.md`.

### Fixed
- Six ruff findings in `src/` and ten in `tests/`, which had never been in
  scope. One was not lint: a `t_int` flag in `_infer_columns` assigned and never
  read. Type inference had a single assertion covering it, so tests for all four
  outcomes went in first — bool is an int subclass and must widen to INTEGER —
  and the flag was then removed.

### Security
- `configs/brave-search.toml` held a live `BRAVE_API_KEY` inline. Never
  committed, never published — confirmed absent from the 0.2.2 sdist, the wheel,
  and all git history — but present in the working tree, which is what hatchling
  builds from. Moved to a gitignored `configs/*.local.toml`.

## [0.2.2] — 2026-09-01

### Fixed
- **Streaming upstream errors reached the client as an empty turn.** Error dicts
  were handed to the *completion* encoders: `encode_as_sse` walks `choices` and
  `encode_message_as_sse` walks `content`, and an error has neither, so the
  client received a well-formed chunk carrying nothing and the OpenAI SDK raised
  a bare `RuntimeError` with the upstream message gone. Reported and fixed for
  the two OpenAI streaming paths by @iamfoz (#5).
- **The same defect at three more sites, found reviewing #5.** The OpenAI
  bad-upstream-kind path (`openai_route.py`) had the identical empty-chunk
  failure four lines above the contributed fix. Both Anthropic streaming error
  paths, plus its bad-upstream-kind path, dressed the failure as an assistant
  message whose text happened to be JSON — which an SDK reads as a successful
  reply, not an error.
- **Upstream status codes are no longer flattened to 502.** All four sites
  hardcoded it, so a 429 rate limit arrived as a server error and clients could
  not tell a throttle from an outage. Each now returns what the upstream sent.

### Added
- `encode_error_as_sse()` in `gateway/anthropic_sse.py` emits a real Anthropic
  `error` event rather than a synthetic assistant turn.
- `tests/gateway/test_streaming_errors.py` covers all four paths. Verified to
  fail against the pre-fix code, not merely to pass against the new.

### Fixed
- **`_meta.cost_avoided` overstated Claude Opus savings by 3x.**
  `_MODEL_PRICES_PER_1M["claude_opus"]` was $15.00/MTok, the retired Opus
  4.1/4.0 input rate. Every current Opus — 5, 4.8, 4.7, 4.6 — is $5.00/MTok, so
  the dollar figure stamped on every tool response was three times the real
  avoided cost for the default model most users run. Corrected to $5.00,
  verified 2026-09-01 against the Base Input Tokens column on
  platform.claude.com/docs/en/about-claude/pricing.
- **`claude_sonnet` was priced from a rate increase that never happened.**
  The table held $3.00/MTok, which is Sonnet 4.6's rate and was also the
  scheduled 2026-09-01 increase for Sonnet 5. Anthropic cancelled that
  increase; $2.00/MTok is now the standard Sonnet 5 price. Corrected to $2.00.
  A constant written for a future date is wrong for the whole interval before
  it and reads identically to a stale one — the date next to it is what makes
  the wrong value look checked.

### Unchanged
- `gpt5_latest` stays at $10.00/MTok. It is not an Anthropic model and no
  verified first-party source was consulted for this change, so the value was
  left alone rather than adjusted on a guess.
- The three keys keep their existing names. They are emitted verbatim in
  `_meta.cost_avoided` on every response; renaming them to spell out the model
  version would break anything parsing that block.

### Added
- `tests/test_meta.py` pins both rates as restated literals plus a
  `cost_avoided()` round-trip, so the next drift fails the suite instead of
  shipping.

## [0.2.1] — 2026-04-30

### Fixed
- **MCP tool names now use underscores (`jmunch_peek` etc.) instead of dots.**
  The Anthropic API enforces `^[a-zA-Z0-9_-]{1,64}$` on tool names server-side,
  so dotted names produced 400s on every Claude Desktop chat that had
  jmunch-mcp loaded (`FrontendRemoteMcpToolDefinition.name` regex error).
  Claude Code happened to mask this because it namespaces remote MCP tools
  as `mcp__<server>__<tool>` before forwarding; Desktop forwards the raw
  `tools/list` names verbatim. Reported by @denovich (#2).
- The seven dotted names (`jmunch.peek`, `jmunch.slice`, `jmunch.search`,
  `jmunch.aggregate`, `jmunch.summarize`, `jmunch.describe`,
  `jmunch.list_handles`) remain accepted as deprecated aliases in the
  dispatcher for one release so in-flight `tools/call` requests from
  older clients still resolve. They are no longer advertised in
  `tools/list`.

## [0.2.0] — 2026-04-24

### Changed
- **Gateway verb loop: compact-iteration architecture.** Drill-in rounds no
  longer re-transmit the accumulating conversation on every upstream call.
  Each verb iteration now forwards a consolidated system message, the
  handle envelope, a terse prior-verbs trail, and the latest verb call +
  result — jmunch-only tool schemas, non-jmunch tools dropped from
  follow-ups. Eliminates the O(K²) context-growth regression that made
  real-world savings a coin flip.
- Dedicated upstream byte counters (`bytes_sent_upstream`,
  `bytes_received_upstream`, `upstream_calls`) on `OpenAIUpstream`.
  Metrics now records actual POSTed bytes, not app-side request size.
- `scrub_params` option on `[[upstream]]` (drops named params before
  forwarding — e.g., Opus 4.7 rejects `temperature` via OpenAI-compat).
- `stream_options` stripped when the gateway forces `stream=false`
  (Anthropic 400 otherwise).

### Added
- Lock-in test `tests/gateway/test_verb_loop_savings.py`. A 100 KB fat
  tool_result across 4 drill-in verbs must land under 35% of raw request
  bytes and stay per-call flat. Guards against re-regression.
- `bench/nanobot/demo/`: two-terminal side-by-side demo (left.sh /
  right.sh) with its own tiny MCP stdio server, per-side metrics DBs,
  and fresh workspaces so the two sides never interfere.

### Fixed
- Metrics schema split (`_SCHEMA_TABLE` + `_SCHEMA_INDEXES`) so
  `ALTER TABLE` migrations run before `CREATE INDEX` — fixes
  "no such column: surface" on databases created pre-0.1.0.
- Dashboard `totals()` accepts `include_zero_savings=True` — needed to
  surface baseline request counts on the OFF side of the demo.

### Performance
- Synthetic benchmark (100 KB tool_result, 4-verb drill-in, measured by
  actual upstream bytes): pre-refactor ~125% of raw request → post-base-
  refactor ~27% → post-optimizations ~24.5%.

## [0.1.0] — 2026-04-23

### Added
- HTTP gateway frontend (`gateway/`) with `/v1/chat/completions` (streaming +
  non-streaming) and `/v1/messages` routes. Token-savings now apply to any
  OpenAI- or Anthropic-compatible app, not just MCP clients.
- Request-side handle-ification of fat `tool_result` blocks; jmunch verb
  injection into `tools` arrays; verb short-circuit with synthesized
  follow-up turns.
- `PersistentHandleRegistry`: SQLite-backed handle store with TTL sweeper,
  survives restarts. In-memory LRU retained as hot cache.
- `TokenCounter`: tiktoken when available, bytes/4 fallback.
- Metrics: `surface` and `tokens_saved_exact` columns (auto-migrated); read
  helpers accept `?surface=mcp|gateway|all` filter.
- `bench/nanobot/`: automated before/after demo wired to Anthropic's
  OpenAI-compat endpoint.
- `[gateway]` and `[exact-tokens]` optional extras keep the base install
  dep-free.
- 35 new tests in `tests/gateway/`.

### Changed
- README expanded with broader MCP server support description.

### Unchanged
- MCP proxy behavior preserved; jMRI-compliant core (sniffer, registry,
  verbs, envelope) shared between MCP and gateway surfaces.

## [0.0.3] — pre-0.1.0

### Changed
- Dashboard hides zero-savings rows uniformly. Any row with `saved_bytes=0`
  no longer surfaces — covers `jmunch.*` handle ops, below-threshold
  passthroughs, and pure errors. Previous tool-prefix filter replaced with
  a single SQL predicate applied to every read query (totals, per_upstream,
  recent_calls, series).

## [0.0.2] — pre-0.1.0

### Added
- Dashboard documentation.

### Fixed
- Python 3.10 compatibility via `tomli` fallback.

## [0.0.1] — initial release

### Added
- Handle-ifying MCP proxy with content-aware backends (JSON, tabular, text).
- Local verbs: `peek`, `slice`, `search`, `summarize`, `aggregate`, `describe`.
- CLI `init` with server discovery and client-config rewrite.
- SQLite metrics store.
- Browser dashboard.

[0.2.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.1.0
[0.0.3]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.3
[0.0.2]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.2
[0.0.1]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.1
