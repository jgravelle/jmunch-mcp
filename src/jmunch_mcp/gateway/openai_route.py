"""`POST /v1/chat/completions` — OpenAI-compatible gateway route (non-streaming).

Request path (app → upstream):
  1. Inspect `messages[]` for `role: "tool"` entries. If a tool message's
     `content` is over the configured threshold, handle-ify it and replace
     content with the jMRI envelope JSON.
  2. Inject jmunch verb tool-definitions into the request's `tools` array
     (per the `inject_tools` mode: auto / always / never).
  3. Forward to the upstream.

Response path (upstream → app):
  1. Inspect `choices[0].message.tool_calls` for jmunch_* verbs.
  2. For each jmunch verb, execute locally via the Dispatcher, append the
     assistant's tool_call and a synthesized tool_result to `messages`,
     re-call the upstream. Repeat until no more jmunch verbs (bounded by
     MAX_VERB_ROUNDS).
  3. Return the final response to the app. The app never sees a jmunch_*
     tool_call — all verb resolution is transparent.

Metrics: one row per app-facing completion, `surface='gateway'`.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any

from ..errors import UPSTREAM_ERROR, make_error
from ..meta import SavingsTracker, envelope, timer_ms
from ..metrics import MetricsDB
from ..registry import HandleRegistry
from ..verbs import Dispatcher
from .config import GatewayConfig
from .handleify import maybe_handleify
from .tool_injection import (
    inject_into_openai_request,
    is_jmunch_gateway_tool,
    to_mcp_name,
)
from .upstreams import Upstream, UpstreamError

log = logging.getLogger("jmunch.gateway.openai")

MAX_VERB_ROUNDS = 8  # safety bound on jmunch verb recursion per completion


def _handleify_request_messages(
    req: dict[str, Any],
    *,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    threshold_tokens: int,
) -> tuple[dict[str, Any], int, list[tuple[str, str]]]:
    """Replace any over-threshold `role: "tool"` message content with a jMRI
    handle envelope.

    Returns (new_request, bytes_saved, raw_sent_pairs) where raw_sent_pairs
    is a list of (raw_text, envelope_text) for each handle-ified message —
    callers can feed these to `TokenCounter` for exact-token accounting.
    """
    messages = req.get("messages")
    if not isinstance(messages, list):
        return req, 0, []

    new_messages: list[Any] = []
    total_saved = 0
    pairs: list[tuple[str, str]] = []
    mutated = False

    for m in messages:
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            new_messages.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, str):
            new_messages.append(m)
            continue
        out = maybe_handleify(
            content, registry=registry, tracker=tracker, threshold_tokens=threshold_tokens
        )
        if out is None:
            new_messages.append(m)
            continue
        env_text, _handle_id = out
        total_saved += max(0, len(content) - len(env_text))
        pairs.append((content, env_text))
        new_m = dict(m)
        new_m["content"] = env_text
        new_messages.append(new_m)
        mutated = True

    if not mutated:
        return req, 0, []
    out_req = dict(req)
    out_req["messages"] = new_messages
    return out_req, total_saved, pairs


def _synthesize_tool_result_text(
    dispatcher: Dispatcher,
    *,
    mcp_name: str,
    args: dict[str, Any],
    tracker: SavingsTracker,
) -> str:
    """Execute a jmunch verb locally and return a JSON string suitable for
    an OpenAI tool message `content`. The payload is the jMRI envelope — same
    shape agents already see from the MCP proxy (jMRI adherence)."""
    started = time.perf_counter_ns()
    result = dispatcher.dispatch(mcp_name, args if isinstance(args, dict) else {})
    is_error = isinstance(result, dict) and "code" in result and "message" in result
    env = envelope(
        result=None if is_error else result,
        error=result if is_error else None,
        raw_bytes=0,
        response_bytes=0,
        tracker=tracker,
        timing_ms=timer_ms(started),
    )
    env_text = json.dumps(env, default=str)
    env["_meta"]["response_tokens"] = len(env_text) // 4
    return json.dumps(env, default=str)


def _jmunch_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    out: list[dict[str, Any]] = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and is_jmunch_gateway_tool(name):
            out.append(c)
    return out


def _parse_tool_call_args(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") or {}
    raw_args = fn.get("arguments")
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _first_turn_streaming(upstream: Upstream, working: dict[str, Any]) -> dict[str, Any]:
    """Call upstream.stream(), parse SSE, assemble into a non-streaming
    response shape. Subsequent turns (after verb resolution) use complete()."""
    from .sse import assemble_response_from_chunks, parse_sse_stream
    events = await parse_sse_stream(upstream.stream(working))
    return assemble_response_from_chunks(events)


# Terse system prompt used for verb-loop iterations only. The model already
# saw the app's full system prompt in the first upstream call; we don't need
# to re-ship it on every drill-in round.
_DRILL_IN_SYSTEM = (
    "You are continuing a task. A large payload has been replaced with a handle; "
    "use jmunch_peek / jmunch_slice / jmunch_search / jmunch_describe / "
    "jmunch_summarize / jmunch_aggregate to drill in further, or answer the user "
    "directly once you have enough information."
)


def _brief(text: str, cap: int = 280) -> str:
    """Short one-line preview of a verb result for the prior-verbs summary."""
    t = text.strip().replace("\n", " ⏎ ")
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _jmunch_only_tools(tools: Any) -> list[Any]:
    """Keep only the jmunch verb tool definitions. In drill-in iterations the
    non-jmunch tools (e.g. the app's original MCP tools) are dead weight —
    the model already made its tool_call to them in the first round."""
    if not isinstance(tools, list):
        return tools
    out: list[Any] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and is_jmunch_gateway_tool(name):
            out.append(t)
    return out


def _compact_envelope_content(content: str) -> str:
    """If `content` is a jMRI envelope for a handle-ified tool_result, strip
    the heavyweight `_meta` block down to just what the model needs (the
    handle id + summary hint). Saves ~500 bytes per drill-in iteration."""
    try:
        env = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content
    if not isinstance(env, dict):
        return content
    result = env.get("result")
    if not (isinstance(result, dict) and isinstance(result.get("handle"), str)):
        return content  # not a handle envelope
    compact = {"handle": result["handle"], "kind": result.get("kind")}
    if "summary" in result:
        compact["summary"] = result["summary"]
    if "_hint" in result:
        compact["_hint"] = result["_hint"]
    return json.dumps(compact)


def _extract_user_and_handle(base: list[Any]) -> tuple[str | None, str | None]:
    """Scan base messages for the user's original question and a handle
    envelope tool_result. Returns (user_text, handle_envelope_text)."""
    user_text: str | None = None
    handle_env: str | None = None
    for m in base:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user" and user_text is None:
            c = m.get("content")
            if isinstance(c, str):
                user_text = c
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            # Keep the most recent handle envelope we find.
            try:
                env = json.loads(m["content"])
                if (isinstance(env, dict)
                    and isinstance(env.get("result"), dict)
                    and isinstance(env["result"].get("handle"), str)):
                    handle_env = _compact_envelope_content(m["content"])
            except (json.JSONDecodeError, ValueError):
                pass
    return user_text, handle_env


def _compact_base_messages(base: list[Any]) -> list[Any]:
    """Prepare `base_messages` for drill-in iterations.

    Aggressive compaction: drop the app's original system prompt, drop the
    app's original tool_call chain, and fold everything the model needs
    into a single system message (drill-in instructions + user question +
    handle info). The result is a minimal `[system]` base; callers append
    `[system_prior_verbs?, assistant(latest_verb), tool(latest_result)]`
    on top of it for each drill-in turn.
    """
    user_text, handle_env = _extract_user_and_handle(base)
    parts = [_DRILL_IN_SYSTEM]
    if user_text:
        parts.append(f"The user's request was:\n{user_text}")
    if handle_env:
        parts.append(f"A large payload has been registered as a handle:\n{handle_env}")
    return [{"role": "system", "content": "\n\n".join(parts)}]


def _prior_verbs_note(trail: list[dict[str, Any]]) -> str:
    lines = []
    for entry in trail:
        lines.append(
            f"- {entry['name']}({entry['args_brief']}) → {entry['result_brief']}"
        )
    return (
        "Earlier in this turn you already called the jmunch verbs below; their "
        "results are not shown in full, only summarised here. Don't repeat a "
        "call whose answer is already in this summary — answer the user's "
        "question directly, or make ONE new drill-in if you still need a specific detail.\n\n"
        + "\n".join(lines)
    )


def _args_brief(args: dict[str, Any]) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        vs = str(v)
        if len(vs) > 40:
            vs = vs[:37] + "…"
        parts.append(f"{k}={vs}")
    return ", ".join(parts)


async def _verb_loop(
    *,
    first_response: dict[str, Any],
    working: dict[str, Any],
    upstream: Upstream,
    dispatcher: Dispatcher,
    tracker: SavingsTracker,
) -> dict[str, Any] | UpstreamError:
    """Given the first upstream response, repeatedly resolve jmunch verb
    tool_calls locally. Each follow-up upstream call carries a **compact**
    message list: the original system/user/assistant-tool-call/tool-result
    chain that the app submitted, plus a running "prior verbs" system note
    (short summaries) and the single most-recent verb call + full result.

    This keeps per-iteration payload roughly flat instead of growing
    O(rounds) — the key to jmunch actually saving tokens when the model
    needs multiple drill-ins to answer.
    """
    base_messages = _compact_base_messages(list(working.get("messages") or []))
    base_tools = _jmunch_only_tools(working.get("tools"))
    verb_trail: list[dict[str, Any]] = []  # past verbs: {name, args_brief, result_brief}
    response = first_response
    rounds = 0
    while True:
        choices = response.get("choices") or []
        if not choices:
            return response
        message = choices[0].get("message") or {}
        jmunch_calls = _jmunch_tool_calls(message)
        if not jmunch_calls or rounds >= MAX_VERB_ROUNDS:
            return response

        # Resolve ALL verbs the model requested in this message. If the model
        # asked for several at once, we service them all before the next call.
        latest_results: list[tuple[dict[str, Any], str]] = []
        for call in jmunch_calls:
            mcp_name = to_mcp_name((call.get("function") or {}).get("name", ""))
            if mcp_name is None:
                continue
            args = _parse_tool_call_args(call)
            tool_result_text = _synthesize_tool_result_text(
                dispatcher, mcp_name=mcp_name, args=args, tracker=tracker,
            )
            latest_results.append((call, tool_result_text))
            log.info("jmunch verb resolved locally: %s (round %d)", mcp_name, rounds + 1)

        # Shift all but the last verb of this round into the running trail
        # (short summaries only). Keep the last verb's full result inline so
        # the model can act on it without re-calling.
        if len(latest_results) > 1:
            for call, result_text in latest_results[:-1]:
                fn = call.get("function") or {}
                verb_trail.append({
                    "name": fn.get("name", "?"),
                    "args_brief": _args_brief(_parse_tool_call_args(call)),
                    "result_brief": _brief(result_text),
                })
        last_call, last_result = latest_results[-1]

        # Build compact messages: base + optional prior-verbs note + single
        # latest verb call + its full result.
        compact_messages: list[Any] = list(base_messages)
        if verb_trail:
            compact_messages.append({
                "role": "system",
                "content": _prior_verbs_note(verb_trail),
            })
        compact_messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": [last_call],
        })
        compact_messages.append({
            "role": "tool",
            "tool_call_id": last_call.get("id", ""),
            "content": last_result,
        })

        # After sending, the latest becomes part of the trail for next round.
        fn = last_call.get("function") or {}
        verb_trail.append({
            "name": fn.get("name", "?"),
            "args_brief": _args_brief(_parse_tool_call_args(last_call)),
            "result_brief": _brief(last_result),
        })

        next_request = dict(working)
        next_request["messages"] = compact_messages
        if base_tools is not None:
            next_request["tools"] = base_tools

        try:
            response = await upstream.complete(next_request)
        except UpstreamError as e:
            return e
        rounds += 1


async def handle_chat_completions(
    req_body: dict[str, Any],
    *,
    upstream_override: str | None,
    config: GatewayConfig,
    upstream_factory,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    dispatcher: Dispatcher,
    metrics: MetricsDB,
    token_counter=None,
) -> tuple[int, dict[str, Any]]:
    """Non-streaming path. For streaming, see `stream_chat_completions`."""
    if req_body.get("stream"):
        return 400, {
            "error": {
                "message": "use stream_chat_completions for stream=true",
                "type": "jmunch_internal",
            }
        }

    model = req_body.get("model")
    model_s = model if isinstance(model, str) else None
    spec = config.resolve_upstream(header=upstream_override, model=model_s)
    if spec.kind != "openai":
        return 400, {
            "error": {
                "message": f"upstream '{spec.name}' is kind={spec.kind}; "
                           "this route requires an openai-compatible upstream",
                "type": "jmunch_bad_upstream",
            }
        }

    started_ns = time.perf_counter_ns()
    raw_request_bytes = len(json.dumps(req_body, default=str))

    prepped, saved_on_request, raw_sent_pairs = _handleify_request_messages(
        req_body,
        registry=registry,
        tracker=tracker,
        threshold_tokens=config.interception.threshold_tokens,
    )
    prepped = inject_into_openai_request(prepped, mode=config.interception.inject_tools)
    exact_saved = _exact_savings(raw_sent_pairs, token_counter, model_s)

    upstream: Upstream = upstream_factory(spec)
    working = copy.deepcopy(prepped)
    final_response: dict[str, Any]

    try:
        try:
            response = await upstream.complete(working)
        except UpstreamError as e:
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {e.status}",
                             status=e.status)
            return 502, {"error": err}

        loop_result = await _verb_loop(
            first_response=response, working=working,
            upstream=upstream, dispatcher=dispatcher, tracker=tracker,
        )
        if isinstance(loop_result, UpstreamError):
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {loop_result.status}",
                             status=loop_result.status)
            return 502, {"error": err}
        final_response = loop_result
    finally:
        await upstream.close()

    response_bytes = len(json.dumps(final_response, default=str))
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    metrics.record(
        upstream=spec.name,
        tool="chat.completions",
        request_bytes=raw_request_bytes,
        raw_bytes=raw_request_bytes + saved_on_request,
        response_bytes=response_bytes,
        saved_bytes=saved_on_request,
        duration_ms=duration_ms,
        handle_created=saved_on_request > 0,
        is_error=False,
        surface="gateway",
        tokens_saved_exact=exact_saved,
        upstream_bytes_sent=getattr(upstream, "bytes_sent_upstream", 0),
        upstream_bytes_received=getattr(upstream, "bytes_received_upstream", 0),
        upstream_calls=getattr(upstream, "upstream_calls", 0),
    )
    return 200, final_response


def _exact_savings(pairs, token_counter, model):
    if token_counter is None or not pairs:
        return 0
    total = 0
    for raw, sent in pairs:
        total += token_counter.count_saved(raw, sent, model=model)
    return total


async def stream_chat_completions(
    req_body: dict[str, Any],
    *,
    upstream_override: str | None,
    config: GatewayConfig,
    upstream_factory,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    dispatcher: Dispatcher,
    metrics: MetricsDB,
    token_counter=None,
):
    """Streaming path. Yields (status, iter_of_bytes).

    Flow: first upstream call is real SSE → parsed via sse.parse_sse_stream →
    assembled to a ChatCompletion shape → verb loop (non-streaming follow-ups)
    → final response re-encoded as SSE chunks.

    Buffer-then-replay: client gets bytes only after the verb loop resolves.
    Acceptable tradeoff for Phase 2 per the plan.
    """
    from .sse import encode_as_sse
    if not req_body.get("stream"):
        # Caller shouldn't be invoking this path for non-streaming requests.
        status, resp = await handle_chat_completions(
            req_body,
            upstream_override=upstream_override, config=config,
            upstream_factory=upstream_factory, registry=registry,
            tracker=tracker, dispatcher=dispatcher, metrics=metrics,
        )
        return status, encode_as_sse(resp)

    model = req_body.get("model")
    model_s = model if isinstance(model, str) else None
    spec = config.resolve_upstream(header=upstream_override, model=model_s)
    if spec.kind != "openai":
        resp = {"error": {
            "message": f"upstream '{spec.name}' is kind={spec.kind}",
            "type": "jmunch_bad_upstream",
        }}
        return 400, encode_as_sse(resp)

    started_ns = time.perf_counter_ns()
    raw_request_bytes = len(json.dumps(req_body, default=str))

    prepped, saved_on_request, raw_sent_pairs = _handleify_request_messages(
        req_body,
        registry=registry, tracker=tracker,
        threshold_tokens=config.interception.threshold_tokens,
    )
    prepped = inject_into_openai_request(prepped, mode=config.interception.inject_tools)
    exact_saved = _exact_savings(raw_sent_pairs, token_counter, model_s)
    # Ensure upstream sees stream=true for the first turn.
    prepped = dict(prepped)
    prepped["stream"] = True

    upstream: Upstream = upstream_factory(spec)
    working = copy.deepcopy(prepped)

    try:
        try:
            first = await _first_turn_streaming(upstream, working)
        except UpstreamError as e:
            log.warning("upstream %s %d: %s", spec.name, e.status, e.body[:500])
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {e.status}: {e.body[:300]}",
                             status=e.status)
            return e.status, [b"data: " + json.dumps(err).encode("utf-8") + b"\n\n", b"data: [DONE]\n\n"]

        loop_result = await _verb_loop(
            first_response=first, working=working,
            upstream=upstream, dispatcher=dispatcher, tracker=tracker,
        )
        if isinstance(loop_result, UpstreamError):
            log.warning("verb-loop upstream %s %d: %s", spec.name, loop_result.status,
                        loop_result.body[:500])
            err = make_error(UPSTREAM_ERROR,
                             f"upstream {spec.name} returned {loop_result.status}: {loop_result.body[:300]}",
                             status=loop_result.status)
            return loop_result.status, [b"data: " + json.dumps(err).encode("utf-8") + b"\n\n", b"data: [DONE]\n\n"]
        final_response = loop_result
    finally:
        await upstream.close()

    chunks = encode_as_sse(final_response)
    response_bytes = sum(len(c) for c in chunks)
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    metrics.record(
        upstream=spec.name,
        tool="chat.completions",
        request_bytes=raw_request_bytes,
        raw_bytes=raw_request_bytes + saved_on_request,
        response_bytes=response_bytes,
        saved_bytes=saved_on_request,
        duration_ms=duration_ms,
        handle_created=saved_on_request > 0,
        is_error=False,
        surface="gateway",
        tokens_saved_exact=exact_saved,
        upstream_bytes_sent=getattr(upstream, "bytes_sent_upstream", 0),
        upstream_bytes_received=getattr(upstream, "bytes_received_upstream", 0),
        upstream_calls=getattr(upstream, "upstream_calls", 0),
    )
    return 200, chunks


