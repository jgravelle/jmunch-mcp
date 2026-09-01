# jmunch-mcp

Token-reduction proxy for MCP. Wraps an upstream server, forwards every call, and
replaces oversized responses with a summary plus an opaque **handle** the model
drills into using a fixed verb set.

Two surfaces, one mechanism:

- **MCP proxy** (v1) — spawns one upstream MCP server as a child and intercepts
  its responses. Configured per upstream in `configs/*.toml`.
- **Gateway** (v2, `[gateway]` extra) — an HTTP server speaking both the OpenAI
  and Anthropic APIs. Point `OPENAI_API_BASE` or `ANTHROPIC_BASE_URL` at it and
  any app gets the same treatment with no code change.

Verbs: `peek`, `slice`, `search`, `aggregate`, `describe`, `list_handles`, plus
`summarize` in gateway mode. Backends route by content shape — SQLite for
tabular, JSON-tree for nested JSON (`slice` takes JSONPath), text for the rest.

## Where this sits relative to the trio

⚠ **This is not a fourth Munch.** jcodemunch / jdocmunch / jdatamunch are domain
retrieval engines that each understand their own domain. jmunch-mcp understands
none, so it classifies payloads by *shape* and applies the same handle-and-verbs
pattern to servers never built for it.

Three concrete couplings, none of them a dependency:

1. **The trio are ordinary upstreams.** `configs/jcodemunch.toml`,
   `jdocmunch.toml`, and `jdatamunch.toml` sit in the same list as GitHub and
   Firecrawl.
2. **Shared envelope spec.** `meta.py` implements the jMRI `_meta` envelope —
   `tokens_saved`, `cost_avoided`, cumulative totals persisted to
   `~/.jmunch/_savings.json`, bytes/4 token approximation. Same contract the trio
   stamp on their responses, which is why a pricing error here has counterparts
   there.
3. **The verbs went upstream.** The trio now expose `jmunch_*` natively as their
   own tools, so they hand back handles without a proxy in the path.

⚠ Before concluding the trio route *through* this proxy, check the actual
topology. Registering them directly in `.claude.json` while their responses carry
`powered_by: jmunch-mcp` means a shared response layer, not a proxy hop.

## Conventions

- **Tests:** `PYTHONPATH=src python -m pytest tests/ -q`. Never omit
  `PYTHONPATH` — the installed package shadows `src/` and you test the wrong
  code.
- **Tool names use underscores.** `jmunch_peek`, never `jmunch.peek`. The
  Anthropic API enforces `^[a-zA-Z0-9_-]{1,64}$` server-side, so dotted names
  400 on every Claude Desktop chat. Dotted aliases stay accepted as deprecated.
  See CHANGELOG 0.2.1.
- **Releasing:** read `RELEASING.md` first. This repo has **no CI** — no
  workflows, nothing that runs ruff, nothing that gates an upload.

## What this repo does not have

Stated because their absence is easy to mistake for a gap in your search:

- **No CI.** Zero workflows (jcodemunch has 11). The local suite is the only
  gate that exists. Step 8 of the shared `release` skill — "read the CI run" —
  has nothing to read here.
- **No CLA app.** A fork PR showing zero commit statuses means no app is
  installed, *not* an unsigned agreement. The shared skill's "absent means NOT
  SIGNED" rule was measured on repos where CLA Assistant actually posts.
- **No `server.json`, and not in the MCP registry.** Verified 2026-09-01: the
  query returns zero rows while the same query returns 57 for jcodemunch, so the
  zero is real rather than the nested-parse false negative. First publish is
  separate work, not part of a release.
- **One version pin site** — `pyproject.toml`. No `plugin.json`, `uv.lock`, or
  `whatsnew.json`. Re-run the enumeration anyway; the list grows.

## Standing hazards

⚠⚠ **The build reads the working tree, not `HEAD`.** Uncommitted edits to
tracked files ship with their edits, and a file that is untracked *and*
unignored ships too. `pyproject.toml` declares no sdist target, so contents fall
back to hatchling's default — which does honor `.gitignore`, and that is the
only thing keeping scratch files out. The protection is incidental, not
declared.

⚠⚠ **Real credentials belong in `configs/*.local.toml`, which is gitignored.**
On 2026-09-01 `configs/brave-search.toml` was found holding a live
`BRAVE_API_KEY` in an `[upstream.env]` block — uncommitted, never shipped, but
present in the tree for months. The 0.2.2 sdist was clean only because the tree
happened to be stashed before the build. Inline credentials in a built artifact
are what got five jcodemunch releases yanked.

⚠ **Do not price a model family from whichever member you last looked at.**
`_MODEL_PRICES_PER_1M` in `meta.py` is emitted verbatim in `_meta.cost_avoided`
on every response, so a wrong rate is user-visible on every call. It held $15
Opus (retired 4.1's rate, 3x over) and $3 Sonnet (4.6's rate, and a Sonnet 5
increase that was cancelled) until 2026-09-01. **A constant written for a FUTURE
date is wrong for the whole interval before it and reads identically to a stale
one** — a date beside a value is what makes a wrong value look checked. The keys
are wire-visible; put the model identity in the comment, never in a rename.
`tests/test_meta.py` pins the rates as restated literals.

⚠ **Error dicts are not completion objects.** `encode_as_sse` walks `choices`;
`encode_message_as_sse` walks `content`. An error has neither, so passing one
through either encoder yields a well-formed chunk carrying nothing, and the
client's SDK raises with the upstream message gone. Five sites did this. Use
`encode_error_as_sse` for Anthropic, and return the upstream's own status —
never a blanket 502, which makes a 429 throttle indistinguishable from an
outage. `tests/gateway/test_streaming_errors.py` covers all four paths.

## Definition of done

A completed work item updates this file and `CHANGELOG.md` in the same step, not
as a separate pass afterwards.
