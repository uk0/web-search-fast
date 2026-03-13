"""Search log middleware — records ALL MCP requests to the database."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Only log requests to MCP endpoints
_LOG_PREFIXES = ("/mcp",)


class SearchLogMiddleware:
    """Pure ASGI middleware that logs ALL MCP requests with request/response bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path

        if not any(path.startswith(p) for p in _LOG_PREFIXES):
            await self.app(scope, receive, send)
            return

        # --- Read and buffer the full request body ---
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            body = message.get("body", b"")
            if body:
                body_chunks.append(body)
            if not message.get("more_body", False):
                break

        request_body = b"".join(body_chunks)

        # Create a new receive that replays the buffered body
        body_sent = False

        async def receive_replay() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            # After body is consumed, wait for disconnect
            return await receive()

        # --- Capture response status and body ---
        status_code = 0
        response_chunks: list[bytes] = []

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
            await send(message)

        t0 = time.monotonic()
        try:
            await self.app(scope, receive_replay, send_wrapper)
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            try:
                asyncio.create_task(self._log_request(
                    request, request_body, response_chunks, status_code, elapsed_ms,
                ))
            except Exception as e:
                logger.debug("Failed to schedule log request: %s", e)

    async def _log_request(
        self,
        request: Request,
        request_body: bytes,
        response_chunks: list[bytes],
        status_code: int,
        elapsed_ms: int,
    ) -> None:
        ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "")
        api_key_id = getattr(request.state, "api_key_id", None)

        request_body_str = request_body.decode("utf-8", errors="replace")
        response_body_str = b"".join(response_chunks).decode("utf-8", errors="replace")
        # Truncate large bodies to avoid bloating the database
        if len(request_body_str) > 10_000:
            request_body_str = request_body_str[:10_000] + "... (truncated)"
        if len(response_body_str) > 10_000:
            response_body_str = response_body_str[:10_000] + "... (truncated)"

        # Parse MCP JSON-RPC to extract tool name, query, engine
        query_text = ""
        engine = ""
        tool_name = ""
        if request_body_str:
            try:
                body = json.loads(request_body_str)
                method = body.get("method", "")
                tool_name = method
                params = body.get("params", {})
                if method == "tools/call":
                    tool_name = params.get("name", method)
                    args = params.get("arguments", {})
                    query_text = args.get("query", "")
                    engine = args.get("engine", "")
                    # Default engine for web_search when client omits it
                    if tool_name == "web_search" and not engine:
                        engine = "duckduckgo"
                elif "arguments" in params:
                    args = params["arguments"]
                    query_text = args.get("query", "")
                    engine = args.get("engine", "")
            except (json.JSONDecodeError, AttributeError):
                pass

        # Use tool_name as query fallback for non-search calls
        if not query_text:
            query_text = tool_name or request.url.path

        from src.admin.repository import log_search
        await log_search(
            query=query_text,
            ip_address=ip,
            engine=engine or None,
            api_key_id=api_key_id,
            user_agent=user_agent,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            request_body=request_body_str or None,
            response_body=response_body_str or None,
            tool_name=tool_name or None,
        )
