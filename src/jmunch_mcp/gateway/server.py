"""aiohttp server entry point for the jmunch gateway.

Wires the shared core (`HandleRegistry`, `SavingsTracker`, `Dispatcher`,
`MetricsDB`) into an HTTP handler. `aiohttp` is imported lazily so the
package still imports cleanly without the [gateway] extra.
"""
from __future__ import annotations

import json
import logging

from ..meta import SavingsTracker
from ..metrics import MetricsDB
from ..persistent_registry import PersistentHandleRegistry
from ..stats import SessionStats
from ..token_counter import TokenCounter
from ..verbs import Dispatcher
from .config import GatewayConfig, UpstreamSpec
from .anthropic_route import handle_messages, stream_messages
from .openai_route import handle_chat_completions, stream_chat_completions
from .upstreams import build as build_upstream

log = logging.getLogger("jmunch.gateway.server")


class GatewayApp:
    """Holds shared core objects. Bound to the aiohttp app via app["jmunch"]."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.registry = PersistentHandleRegistry(
            store_path=config.handles.store_path,
            ttl_seconds=config.handles.ttl_seconds,
            max_bytes=config.handles.max_bytes,
        )
        self.tracker = SavingsTracker()
        self.stats = SessionStats(tokens_saved_at_start=self.tracker.total)
        self.dispatcher = Dispatcher(self.registry, self.stats)
        self.metrics = MetricsDB()
        self.token_counter = TokenCounter()

    def upstream_factory(self, spec: UpstreamSpec):
        return build_upstream(spec)


def _parse_listen(listen: str) -> tuple[str, int]:
    if ":" not in listen:
        raise ValueError(f"invalid listen address: {listen!r} (expected host:port)")
    host, port_s = listen.rsplit(":", 1)
    return host, int(port_s)


def build_aiohttp_app(gateway: GatewayApp):
    """Construct the aiohttp application. Imports aiohttp lazily."""
    try:
        from aiohttp import web
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "aiohttp is required for the gateway. "
            "Install with: pip install 'jmunch-mcp[gateway]'"
        ) from e

    app = web.Application(client_max_size=64 * 1024 * 1024)  # 64 MB request cap
    app["jmunch"] = gateway

    async def openai_chat(request):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)
        header = request.headers.get("X-Jmunch-Upstream")
        inject_header = request.headers.get("X-Jmunch-Inject")
        # Per-request inject-override: `X-Jmunch-Inject: false` → never.
        config = gateway.config
        if inject_header and inject_header.lower() in ("false", "0", "no"):
            config_for_call = _with_inject_mode(config, "never")
        else:
            config_for_call = config

        if body.get("stream"):
            status, chunks = await stream_chat_completions(
                body,
                upstream_override=header,
                config=config_for_call,
                upstream_factory=gateway.upstream_factory,
                registry=gateway.registry,
                tracker=gateway.tracker,
                dispatcher=gateway.dispatcher,
                metrics=gateway.metrics,
            )
            resp = web.StreamResponse(
                status=status,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
            await resp.prepare(request)
            for c in chunks:
                await resp.write(c)
            await resp.write_eof()
            return resp

        status, resp = await handle_chat_completions(
            body,
            upstream_override=header,
            config=config_for_call,
            upstream_factory=gateway.upstream_factory,
            registry=gateway.registry,
            tracker=gateway.tracker,
            dispatcher=gateway.dispatcher,
            metrics=gateway.metrics,
            token_counter=gateway.token_counter,
        )
        return web.json_response(resp, status=status)

    async def anthropic_messages(request):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response(
                {"type": "error", "error": {"message": "invalid JSON body"}},
                status=400,
            )
        header = request.headers.get("X-Jmunch-Upstream")
        inject_header = request.headers.get("X-Jmunch-Inject")
        config_for_call = gateway.config
        if inject_header and inject_header.lower() in ("false", "0", "no"):
            config_for_call = _with_inject_mode(gateway.config, "never")

        if body.get("stream"):
            status, chunks = await stream_messages(
                body,
                upstream_override=header,
                config=config_for_call,
                upstream_factory=gateway.upstream_factory,
                registry=gateway.registry,
                tracker=gateway.tracker,
                dispatcher=gateway.dispatcher,
                metrics=gateway.metrics,
            )
            resp = web.StreamResponse(
                status=status,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
            await resp.prepare(request)
            for c in chunks:
                await resp.write(c)
            await resp.write_eof()
            return resp

        status, resp = await handle_messages(
            body,
            upstream_override=header,
            config=config_for_call,
            upstream_factory=gateway.upstream_factory,
            registry=gateway.registry,
            tracker=gateway.tracker,
            dispatcher=gateway.dispatcher,
            metrics=gateway.metrics,
            token_counter=gateway.token_counter,
        )
        return web.json_response(resp, status=status)

    async def models_passthrough(request):
        # Phase 1: return an empty list. Apps that don't call this endpoint
        # (most do not after the first handshake) are unaffected.
        return web.json_response({"object": "list", "data": []})

    async def health(request):
        return web.json_response({
            "status": "ok",
            "upstreams": [u.name for u in gateway.config.upstreams],
            "tokens_saved_total": gateway.tracker.total,
        })

    async def _on_startup(_app):
        await gateway.registry.start_sweeper()

    async def _on_cleanup(_app):
        await gateway.registry.stop_sweeper()
        gateway.registry.close_db()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_post("/v1/chat/completions", openai_chat)
    app.router.add_post("/v1/messages", anthropic_messages)
    app.router.add_get("/v1/models", models_passthrough)
    app.router.add_get("/health", health)
    return app


def _with_inject_mode(config: GatewayConfig, mode: str) -> GatewayConfig:
    from dataclasses import replace
    return replace(config, interception=replace(config.interception, inject_tools=mode))


def serve(config: GatewayConfig) -> int:
    """Blocking entrypoint for `jmunch-mcp gateway`."""
    try:
        from aiohttp import web
    except ImportError:  # pragma: no cover
        print(
            "error: aiohttp missing. Install with: pip install 'jmunch-mcp[gateway]'",
            flush=True,
        )
        return 2

    host, port = _parse_listen(config.listen)
    gateway = GatewayApp(config)
    app = build_aiohttp_app(gateway)
    log.info("jmunch gateway listening on http://%s:%d", host, port)
    try:
        web.run_app(app, host=host, port=port, print=None)
    except KeyboardInterrupt:
        return 130
    return 0
