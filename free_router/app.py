from __future__ import annotations

import asyncio
import hmac
import json
import math
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp
from aiohttp import web

from .config import Config, ConfigError
from .state import State


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def error_response(status, code, message):
    return web.json_response({"error": {"type": "router_error", "code": code, "message": message}}, status=status)


def strict_object(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Nonfinite number")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def validate_request(body):
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "Expected JSON object")
    allowed = {"model", "messages", "max_tokens", "temperature", "top_p", "stream", "response_format", "tools", "tool_choice", "stop", "seed", "router"}
    if set(body) - allowed:
        raise ApiError(400, "unsupported_field", "Unsupported request fields")
    if not isinstance(body.get("model"), str):
        raise ApiError(400, "invalid_model", "model is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise ApiError(400, "invalid_messages", "1-256 messages required")
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ApiError(400, "invalid_messages", "Invalid message role")
        if set(item) - {"role", "content", "tool_calls", "tool_call_id", "name"}:
            raise ApiError(400, "invalid_messages", "Unsupported message field")
        if not isinstance(item.get("content"), str) and not (item.get("role") == "assistant" and item.get("tool_calls") and item.get("content") is None):
            raise ApiError(400, "text_only", "Only text messages and assistant tool calls are supported")
        if item["role"] == "tool" and not isinstance(item.get("tool_call_id"), str):
            raise ApiError(400, "invalid_tool", "tool_call_id required")
        if "tool_calls" in item and not valid_tool_calls(item["tool_calls"]):
            raise ApiError(400, "invalid_tool", "Invalid assistant tool calls")
    max_tokens = body.get("max_tokens", 4096)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 131072:
        raise ApiError(400, "invalid_tokens", "max_tokens must be 1-131072")
    if type(body.get("stream", False)) is not bool:
        raise ApiError(400, "invalid_stream", "stream must be boolean")
    for field, high in [("temperature", 2), ("top_p", 1)]:
        value = body.get(field, 1)
        if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= high:
            raise ApiError(400, "invalid_sampling", "Invalid sampling parameter")
    fmt = body.get("response_format", {"type": "text"})
    if fmt not in ({"type": "text"}, {"type": "json_object"}):
        raise ApiError(400, "unsupported_format", "Only text and json_object supported")
    # A whole JSON object can be verified only after buffering it.
    if body.get("stream") and fmt["type"] == "json_object":
        raise ApiError(400, "json_requires_buffering", "json_object requires stream=false")
    if "tools" in body:
        if not isinstance(body["tools"], list) or not 1 <= len(body["tools"]) <= 32:
            raise ApiError(400, "invalid_tools", "1-32 function tools required")
        for tool in body["tools"]:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict) or not isinstance(tool["function"].get("name"), str):
                raise ApiError(400, "invalid_tools", "Only named function tools supported")
    if "tool_choice" in body and "tools" not in body:
        raise ApiError(400, "invalid_tools", "tool_choice requires tools")
    options = body.get("router", {})
    if not isinstance(options, dict) or set(options) - {"exclude_families"}:
        raise ApiError(400, "invalid_routing", "Unknown router option")
    excluded = options.get("exclude_families", [])
    if not isinstance(excluded, list) or len(excluded) > 64 or not all(isinstance(x, str) for x in excluded):
        raise ApiError(400, "invalid_routing", "Invalid excluded families")
    return {**body, "max_tokens": max_tokens, "stream": body.get("stream", False)}


def valid_tool_calls(calls):
    return isinstance(calls, list) and bool(calls) and all(
        isinstance(c, dict) and isinstance(c.get("id"), str) and c.get("type") == "function"
        and isinstance(c.get("function"), dict) and isinstance(c["function"].get("name"), str)
        and isinstance(c["function"].get("arguments"), str) for c in calls)


def retry_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 30
    return min(86400, max(1, delay)) if math.isfinite(delay) else 30


async def bounded_read(response, maximum):
    data = bytearray()
    async for part in response.content.iter_chunked(65536):
        data.extend(part)
        if len(data) > maximum:
            raise ValueError("Upstream response limit")
    return data


class Router:
    def __init__(self, config, state, config_path=None):
        self.config = config
        self.config_path = config_path
        self.state = state
        self.active = 0
        self.client_active = {}
        self.session = None

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        for client in self.config.clients.values():
            if client["token"] and hmac.compare_digest(supplied.encode(), client["token"].encode()):
                return client
        raise ApiError(401, "unauthorized", "Valid client credential required")

    def candidates(self, body, client):
        cfg = self.config
        name = body["model"]
        if name not in client["allowed_models"]:
            raise ApiError(403, "model_not_allowed", "Model is not permitted for this client")
        routes = cfg.aliases.get(name, [name] if name in cfg.models else [])
        required = {"text"}
        if body.get("response_format", {}).get("type") == "json_object": required.add("json")
        if body.get("stream"): required.add("stream")
        if body.get("tools") or any(x.get("tool_calls") or x["role"] == "tool" for x in body["messages"]): required.add("tools")
        # UTF-8 bytes + framing is intentionally conservative, not a billing meter.
        input_bound = len(json.dumps({"messages": body["messages"], "tools": body.get("tools", [])}, ensure_ascii=False).encode()) + 32*len(body["messages"])
        exclude = body.get("router", {}).get("exclude_families", [])
        return [cfg.models[r] for r in routes if cfg.eligible(cfg.models[r])
                and required <= set(cfg.models[r]["capabilities"])
                and cfg.models[r]["family"] not in exclude
                and body["max_tokens"] <= cfg.models[r]["max_output_tokens"]
                and input_bound + body["max_tokens"] <= cfg.models[r]["context_tokens"]]

    async def models(self, request):
        client = self.authenticate(request)
        data = []
        for name in client["allowed_models"]:
            ids = self.config.aliases.get(name, [name])
            if any(self.config.eligible(self.config.models[x]) for x in ids):
                data.append({"id": name, "object": "model", "owned_by": "free-model-router"})
        return web.json_response({"object": "list", "data": data})

    async def usage(self, request):
        return web.json_response({"data": self.state.summary(self.authenticate(request)["id"])})

    async def ready(self, request):
        ready = any(c["token"] and any(self.config.eligible(self.config.models[r])
                for name in c["allowed_models"] for r in self.config.aliases.get(name, [name]))
                for c in self.config.clients.values())
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    async def reload(self, request):
        token = self.config.env.get("ROUTER_ADMIN_KEY", "")
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if len(token) < 32 or not hmac.compare_digest(token.encode(), supplied.encode()):
            raise ApiError(401, "unauthorized", "Admin credential required")
        if not self.config_path:
            raise ApiError(409, "reload_unavailable", "No configuration path")
        try:
            config = Config.load(self.config_path, environ=self.config.env, allow_local=self.config.allow_local)
        except (OSError, ValueError, KeyError, TypeError):
            raise ApiError(400, "invalid_configuration", "Existing configuration retained")
        self.config = config
        return web.json_response({"reloaded": True})

    async def completion(self, request):
        client = self.authenticate(request)
        if request.content_type != "application/json":
            raise ApiError(415, "content_type", "application/json required")
        try:
            body = validate_request(strict_object(await request.text()))
        except (ValueError, UnicodeError):
            raise ApiError(400, "invalid_json", "Invalid JSON request")
        cfg = self.config
        routes = self.candidates(body, client)
        if not routes:
            raise ApiError(503, "no_free_route", "No verified free route meets the request requirements")
        cid = client["id"]
        if self.active >= cfg.max_parallel or self.client_active.get(cid, 0) >= client.get("max_parallel", 2):
            raise ApiError(429, "concurrency_limit", "Too many concurrent requests")
        if not self.state.reserve("client:"+cid, client.get("requests_per_minute", 20), client.get("requests_per_day", 500)):
            raise ApiError(429, "client_quota", "Client request limit reached")
        self.active += 1
        self.client_active[cid] = self.client_active.get(cid, 0)+1
        try:
            if body["stream"]:
                # Each attempt uses the remaining total deadline. Once SSE starts,
                # failures must be emitted as SSE, never a second HTTP response.
                return await self.forward(request, body, routes, cfg, cid)
            async with asyncio.timeout(cfg.timeout):
                return await self.forward(request, body, routes, cfg, cid)
        except TimeoutError:
            raise ApiError(504, "deadline_exceeded", "Router total deadline exceeded")
        finally:
            self.active -= 1
            self.client_active[cid] -= 1

    async def forward(self, request, body, routes, cfg, cid):
        attempts = 0
        failures = []
        deadline = time.monotonic() + cfg.timeout
        for route in routes:
            if attempts >= cfg.max_attempts: break
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            # Recheck at the moment of dispatch, not just admission.
            if not cfg.eligible(route): continue
            provider = cfg.providers[route["provider"]]
            scope = "account:"+provider["quota_group"]
            if self.state.blocked(scope) or self.state.blocked("route:"+route["id"]): continue
            # Use the smallest limit across endpoints sharing an account.
            group = [p for p in cfg.providers.values() if p["quota_group"] == provider["quota_group"]]
            if not self.state.reserve(scope, min(p["requests_per_minute"] for p in group), min(p["requests_per_day"] for p in group)): continue
            attempts += 1
            payload = {k:v for k,v in body.items() if k != "router"}
            payload["model"] = route["model"]
            metadata = {"requested_model": body["model"], "route": route["id"], "provider": route["provider"],
                        "model": route["model"], "family": route["family"], "attempts": attempts,
                        "fallback_reasons": list(failures)}
            headers = {"Authorization": "Bearer "+cfg.env[provider["key_env"]], "Content-Type": "application/json"}
            try:
                async with self.session.post(provider["base_url"].rstrip("/")+"/chat/completions",
                      json=payload, headers=headers, allow_redirects=False,
                      timeout=aiohttp.ClientTimeout(total=remaining, sock_connect=min(10,remaining), sock_read=min(30,remaining))) as upstream:
                    if upstream.status != 200:
                        code = upstream.status
                        reason = f"http_{code}"
                        self.state.record(cid, route["id"], reason)
                        failures.append({"route": route["id"], "reason": reason})
                        if code in {401,403,402}: self.state.cool(scope, 86400)
                        elif code == 429: self.state.cool(scope, retry_delay(upstream.headers.get("Retry-After")))
                        else: self.state.cool("route:"+route["id"], 30)
                        # Do not downgrade structured output or follow redirects.
                        continue
                    if body["stream"]:
                        return await self.stream(request, upstream, metadata, cfg, cid)
                    raw = await bounded_read(upstream, cfg.max_response)
                    parsed = strict_object(raw)
                    choices = parsed.get("choices")
                    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                        raise ValueError("Invalid choices")
                    choice = choices[0]
                    msg = choice.get("message")
                    if not isinstance(msg, dict) or msg.get("role", "assistant") != "assistant":
                        raise ValueError("Invalid message")
                    if choice.get("finish_reason") in {"length", "max_tokens", "content_filter"}:
                        raise ValueError("Incomplete response")
                    content, calls = msg.get("content"), msg.get("tool_calls")
                    if calls:
                        if not body.get("tools") or not valid_tool_calls(calls): raise ValueError("Unexpected tool calls")
                    elif not isinstance(content, str) or not content.strip(): raise ValueError("Empty content")
                    if body.get("response_format", {}).get("type") == "json_object": strict_object(content)
                    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
                    # Never relay provider error/debug fields or their upstream identity claims.
                    result = {"id": "chatcmpl-"+uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
                              "model": route["model"], "choices": [{"index":0, "message": {"role":"assistant", "content":content},
                              "finish_reason":choice.get("finish_reason", "stop")}], "usage":clean_usage(usage), "router":metadata}
                    if calls: result["choices"][0]["message"]["tool_calls"] = calls
                    self.state.record(cid, route["id"], "ok", usage)
                    return web.json_response(result)
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError, UnicodeError):
                self.state.record(cid, route["id"], "invalid_or_unavailable")
                failures.append({"route":route["id"], "reason":"invalid_or_unavailable"})
                self.state.cool("route:"+route["id"], 30)
        if time.monotonic() >= deadline:
            raise ApiError(504, "deadline_exceeded", "Router total deadline exceeded")
        raise ApiError(503, "free_routes_exhausted", "All compatible free routes are unavailable")

    async def stream(self, request, upstream, metadata, cfg, cid):
        if upstream.content_type != "text/event-stream":
            raise ValueError("Expected SSE")
        downstream = web.StreamResponse(headers={"Content-Type":"text/event-stream", "Cache-Control":"no-cache",
          "X-Accel-Buffering":"no", "X-Router-Route":metadata["route"], "X-Router-Provider":metadata["provider"]})
        await downstream.prepare(request)
        size, done = 0, False
        try:
            # Preserve framed SSE; never combine output from different models.
            while not upstream.content.at_eof():
                line = await upstream.content.readline()
                size += len(line)
                if size > cfg.max_response or len(line) > 262144: raise ValueError("Stream limit")
                if not line.strip(): continue
                if not line.startswith(b"data:"): continue
                text = line[5:].strip()
                if text == b"[DONE]":
                    done = True
                    await downstream.write(b"data: [DONE]\n\n")
                    break
                chunk = strict_object(text)
                if "error" in chunk or not isinstance(chunk.get("choices"), list): raise ValueError("Invalid stream")
                safe = {"id":"chatcmpl-stream", "object":"chat.completion.chunk", "created":int(time.time()),
                        "model":metadata["model"], "choices":chunk["choices"], "router":metadata}
                await downstream.write(b"data: "+json.dumps(safe,ensure_ascii=False).encode()+b"\n\n")
            if not done: raise ValueError("Truncated stream")
            self.state.record(cid, metadata["route"], "ok_stream")
        except asyncio.CancelledError:
            self.state.record(cid, metadata["route"], "cancelled")
            raise
        except (Exception,):
            self.state.record(cid, metadata["route"], "stream_interrupted")
            try:
                await downstream.write(b'data: {"error":{"code":"stream_interrupted","message":"Stream interrupted; no fallback after output begins"}}\n\n')
            except (ConnectionError, RuntimeError): pass
        try: await downstream.write_eof()
        except (ConnectionError, RuntimeError): pass
        return downstream


def clean_usage(usage):
    return {k:v for k,v in usage.items() if k in {"prompt_tokens","completion_tokens","total_tokens"} and type(v) is int and 0 <= v <= 10000000}


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return error_response(exc.status, exc.code, exc.message)
    except web.HTTPRequestEntityTooLarge:
        return error_response(413, "body_too_large", "Request body exceeds limit")
    except web.HTTPException as exc:
        return error_response(exc.status, "http_error", "Unsupported HTTP request")
    except Exception:
        return error_response(500, "internal_error", "Internal router error")


def create_app(config, *, database=":memory:", config_path=None):
    router = Router(config, State(database), config_path)
    app = web.Application(client_max_size=config.max_body, middlewares=[errors])
    app[ROUTER_KEY] = router
    async def lifecycle(app):
        async with aiohttp.ClientSession(trust_env=True) as session:
            router.session = session
            try: yield
            finally: router.state.close()
    app.cleanup_ctx.append(lifecycle)
    async def health(request): return web.json_response({"ok":True})
    app.add_routes([web.get("/healthz",health), web.get("/readyz",router.ready),
       web.get("/v1/models",router.models), web.get("/v1/usage",router.usage),
       web.post("/v1/chat/completions",router.completion), web.post("/admin/reload",router.reload)])
    return app


ROUTER_KEY = web.AppKey("router", Router)
