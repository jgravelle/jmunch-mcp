"""`POST /v1/messages` — Anthropic-native gateway route.

Anthropic's wire format differs from OpenAI:
  * `messages[].content` is an array of content blocks (text / tool_use /
    tool_result), not a string.
  * Tool calls appear as `tool_use` blocks in assistant responses.
  * Tool results come from the user role as `tool_result` blocks keyed by
    `tool_use_id`.

The jmunch core is reused verbatim (sniffer, backends, registry, dispatcher,
envelope). Only the wire-format-specific glue lives here.
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
from .anthropic_sse import (
    assemble_message_from_events,
    encode_error_as_sse,
    encode_message_as_sse,
    parse_anthropic_sse,
)
from .config import GatewayConfig
from .handleify import maybe_handleify
from .tool_injection import (
    inject_into_anthropic_request,
    is_jmunch_gateway_tool,
    to_mcp_name,
)
from .upstreams import Upstream, UpstreamError

log = logging.getLogger("jmunch.gateway.anthropic")

MAX_VERB_ROUNDS = 8


def _tool_result_text(block: dict[str, Any]) -> str | None:
    """Extract the textual payload from a `tool_result` content block.
    Anthropic allows `content` to be a string OR a list of content blocks.
    Returns None if the payload isn't a plain text carrier we can replace."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # All-text blocks → join their texts. Anything else → skip.
        texts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    texts.append(t)
                else:
                    return None
            else:
                return None
        return "".join(texts)
    return None


def _replace_tool_result_content(block: dict[str, Any], new_text: str) -> dict[str, Any]:
    out = dict(block)
    # Preserve whichever shape the caller used.
    if isinstance(block.get("content"), list):
        out["content"] = [{"type": "text", "text": new_text}]
    else:
        out["content"] = new_text
    return out


def _handleify_request_messages(
    req: dict[str, Any],
    *,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    threshold_tokens: int,
) -> tuple[dict[str, Any], int, list[tuple[str, str]]]:
    messages = req.get("messages")
    if not isinstance(messages, list):
        return req, 0, []

    new_messages: list[Any] = []
    total_saved = 0
    pairs: list[tuple[str, str]] = []
    mutated = False

    for m in messages:
        if not (isinstance(m, dict) and isinstance(m.get("content"), list)):
            new_messages.append(m)
            continue
        blocks = m["content"]
        new_blocks: list[Any] = []
        touched = False
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = _tool_result_text(b)
                if isinstance(text, str):
                    out = maybe_handleify(
                        text, registry=registry, tracker=tracker,
                        threshold_tokens=threshold_tokens,
                    )
                    if out is not None:
                        env_text, _ = out
                        total_saved += max(0, len(text) - len(env_text))
                        pairs.append((text, env_text))
                        new_blocks.append(_replace_tool_result_content(b, env_text))
                        touched = True
                        continue
            new_blocks.append(b)
        if touched:
            new_m = dict(m)
            new_m["content"] = new_blocks
            new_messages.append(new_m)
            mutated = True
        else:
            new_messages.append(m)

    if not mutated:
        return req, 0, []
    out_req = dict(req)
    out_req["messages"] = new_messages
    return out_req, total_saved, pairs


def _exact_savings(pairs, token_counter, model):
    if token_counter is None or not pairs:
        return 0
    total = 0
    for raw, sent in pairs:
        total += token_counter.count_saved(raw, sent, model=model)
    return total


def _jmunch_tool_uses(message_content: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in message_content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") != "tool_use":
            continue
        name = b.get("name")
        if isinstance(name, str) and is_jmunch_gateway_tool(name):
            out.append(b)
    return out


def _synthesize_tool_result_env(
    dispatcher: Dispatcher,
    *,
    mcp_name: str,
    args: dict[str, Any],
    tracker: SavingsTracker,
) -> str:
    started = time.perf_counter_ns()
    result = dispatcher.dispatch(mcp_name, args if isinstance(args, dict) else {})
    is_error = isinstance(result, dict) and "code" in result and "message" in result
    env = envelope(
        result=None if is_error else result,
        error=result if is_error else None,
        raw_bytes=0, response_bytes=0,
        tracker=tracker, timing_ms=timer_ms(started),
    )
    env_text = json.dumps(env, default=str)
    env["_meta"]["response_tokens"] = len(env_text) // 4
    return json.dumps(env, default=str)


async def _verb_loop(
    *,
    first_response: dict[str, Any],
    working: dict[str, Any],
    upstream: Upstream,
    dispatcher: Dispatcher,
    tracker: SavingsTracker,
) -> dict[str, Any] | UpstreamError:
    response = first_response
    rounds = 0
    while True:
        content = response.get("content") or []
        jmunch_uses = _jmunch_tool_uses(content)
        if not jmunch_uses or rounds >= MAX_VERB_ROUNDS:
            return response

        # Append assistant message (its full content) + user tool_result message.
        assistant_msg = {"role": "assistant", "content": copy.deepcopy(content)}
        tool_result_blocks: list[dict[str, Any]] = []
        for use in jmunch_uses:
            mcp_name = to_mcp_name(use.get("name", ""))
            if mcp_name is None:
                continue
            args = use.get("input")
            if not isinstance(args, dict):
                args = {}
            env_text = _synthesize_tool_result_env(
                dispatcher, mcp_name=mcp_name, args=args, tracker=tracker,
            )
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": use.get("id", ""),
                "content": env_text,
            })
            log.info("anthropic jmunch verb resolved: %s (round %d)", mcp_name, rounds + 1)

        working_messages = list(working.get("messages") or [])
        working_messages.append(assistant_msg)
        working_messages.append({"role": "user", "content": tool_result_blocks})
        working["messages"] = working_messages

        try:
            response = await upstream.complete(working)
        except UpstreamError as e:
            return e
        rounds += 1


async def handle_messages(
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
    if req_body.get("stream"):
        return 400, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "use stream_messages for stream=true",
        }}

    model = req_body.get("model")
    model_s = model if isinstance(model, str) else None
    spec = config.resolve_upstream(header=upstream_override, model=model_s)
    if spec.kind != "anthropic":
        return 400, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": f"upstream '{spec.name}' kind={spec.kind}; /v1/messages requires anthropic",
        }}

    started_ns = time.perf_counter_ns()
    raw_request_bytes = len(json.dumps(req_body, default=str))

    prepped, saved_on_request, raw_sent_pairs = _handleify_request_messages(
        req_body, registry=registry, tracker=tracker,
        threshold_tokens=config.interception.threshold_tokens,
    )
    prepped = inject_into_anthropic_request(prepped, mode=config.interception.inject_tools)
    exact_saved = _exact_savings(raw_sent_pairs, token_counter, model_s)

    upstream: Upstream = upstream_factory(spec)
    working = copy.deepcopy(prepped)
    try:
        try:
            first = await upstream.complete(working)
        except UpstreamError as e:
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {e.status}",
                             status=e.status)
            return 502, {"type": "error", "error": err}
        loop_result = await _verb_loop(
            first_response=first, working=working,
            upstream=upstream, dispatcher=dispatcher, tracker=tracker,
        )
        if isinstance(loop_result, UpstreamError):
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {loop_result.status}",
                             status=loop_result.status)
            return 502, {"type": "error", "error": err}
        final = loop_result
    finally:
        await upstream.close()

    response_bytes = len(json.dumps(final, default=str))
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    metrics.record(
        upstream=spec.name,
        tool="messages",
        request_bytes=raw_request_bytes,
        raw_bytes=raw_request_bytes + saved_on_request,
        response_bytes=response_bytes,
        saved_bytes=saved_on_request,
        duration_ms=duration_ms,
        handle_created=saved_on_request > 0,
        is_error=False,
        surface="gateway",
        tokens_saved_exact=exact_saved,
    )
    return 200, final


async def stream_messages(
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
    """Stream from Anthropic, buffer-then-replay with verb short-circuit."""
    if not req_body.get("stream"):
        status, resp = await handle_messages(
            req_body, upstream_override=upstream_override, config=config,
            upstream_factory=upstream_factory, registry=registry,
            tracker=tracker, dispatcher=dispatcher, metrics=metrics,
        )
        return status, encode_message_as_sse(resp)

    model = req_body.get("model")
    model_s = model if isinstance(model, str) else None
    spec = config.resolve_upstream(header=upstream_override, model=model_s)
    if spec.kind != "anthropic":
        return 400, encode_error_as_sse(
            make_error(UPSTREAM_ERROR, f"upstream '{spec.name}' is kind={spec.kind}", status=400)
        )

    started_ns = time.perf_counter_ns()
    raw_request_bytes = len(json.dumps(req_body, default=str))

    prepped, saved_on_request, raw_sent_pairs = _handleify_request_messages(
        req_body, registry=registry, tracker=tracker,
        threshold_tokens=config.interception.threshold_tokens,
    )
    prepped = inject_into_anthropic_request(prepped, mode=config.interception.inject_tools)
    exact_saved = _exact_savings(raw_sent_pairs, token_counter, model_s)
    prepped = dict(prepped)
    prepped["stream"] = True

    upstream: Upstream = upstream_factory(spec)
    working = copy.deepcopy(prepped)
    try:
        try:
            events = await parse_anthropic_sse(upstream.stream(working))
        except UpstreamError as e:
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {e.status}",
                             status=e.status)
            return e.status, encode_error_as_sse(err)
        first = assemble_message_from_events(events)
        loop_result = await _verb_loop(
            first_response=first, working=working,
            upstream=upstream, dispatcher=dispatcher, tracker=tracker,
        )
        if isinstance(loop_result, UpstreamError):
            err = make_error(UPSTREAM_ERROR, f"upstream {spec.name} returned {loop_result.status}",
                             status=loop_result.status)
            return loop_result.status, encode_error_as_sse(err)
        final = loop_result
    finally:
        await upstream.close()

    chunks = encode_message_as_sse(final)
    response_bytes = sum(len(c) for c in chunks)
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    metrics.record(
        upstream=spec.name, tool="messages",
        request_bytes=raw_request_bytes,
        raw_bytes=raw_request_bytes + saved_on_request,
        response_bytes=response_bytes,
        saved_bytes=saved_on_request,
        duration_ms=duration_ms,
        handle_created=saved_on_request > 0,
        is_error=False, surface="gateway",
        tokens_saved_exact=exact_saved,
    )
    return 200, chunks
