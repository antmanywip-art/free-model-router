"""Validated, immutable configuration. Loading never calls an API."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ConfigError(message)


def utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "Timestamps must include timezone")
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError("Invalid timestamp") from exc


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f"Invalid {label}")
    return value


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,159}", value)


def env_name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", value), "Invalid variable name")
    return value


def endpoint(value, *, allow_local=False):
    require(isinstance(value, str), "Endpoint must be a URL")
    parsed = urlsplit(value)
    require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "Endpoint cannot contain credentials, query or fragment")
    require(bool(parsed.hostname), "Endpoint requires hostname")
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    require(parsed.scheme == "https" or (allow_local and local and parsed.scheme == "http"),
            "HTTPS required (loopback HTTP only in explicit test mode)")
    require(allow_local or not local, "Loopback upstreams are test-only")
    return value.rstrip("/")


class Config:
    def __init__(self, raw, *, environ=None, allow_local=False):
        require(isinstance(raw, dict) and raw.get("version") == 1, "Expected configuration version 1")
        self.raw = raw
        self.env = dict(os.environ if environ is None else environ)
        self.allow_local = allow_local
        self.timeout = integer(raw.get("timeout_seconds", 90), 1, 300, "timeout")
        self.max_attempts = integer(raw.get("max_attempts", 3), 1, 8, "attempts")
        self.max_body = integer(raw.get("max_request_bytes", 524288), 1024, 2000000, "body limit")
        self.max_response = integer(raw.get("max_response_bytes", 4000000), 1024, 16000000, "response limit")
        self.max_parallel = integer(raw.get("max_parallel", 2), 1, 16, "parallelism")
        self.clients = {}
        tokens = set()
        for client in raw.get("clients", []):
            require(identifier(client.get("id")) and client["id"] not in self.clients, "Duplicate/invalid client")
            env_name(client.get("key_env"))
            token = self.env.get(client["key_env"], "")
            if token:
                require(len(token) >= 32 and not any(c.isspace() for c in token), "Client keys need 32+ nonspace characters")
                require(token not in tokens, "Each client requires its own key")
                tokens.add(token)
            for key, default in [("requests_per_minute", 20), ("requests_per_day", 500), ("max_parallel", 2)]:
                integer(client.get(key, default), 1, 100000, key)
            require(isinstance(client.get("allowed_models"), list) and bool(client["allowed_models"]), "Client requires allowed_models")
            self.clients[client["id"]] = {**client, "token": token}
        admin = self.env.get("ROUTER_ADMIN_KEY", "")
        if admin:
            require(len(admin) >= 32 and admin not in tokens and not any(c.isspace() for c in admin),
                    "Admin key must be distinct from client keys and 32+ characters")
        self.providers = {}
        for item in raw.get("providers", []):
            require(identifier(item.get("id")) and item["id"] not in self.providers, "Duplicate/invalid provider")
            endpoint(item.get("base_url"), allow_local=allow_local)
            env_name(item.get("key_env"))
            require(identifier(item.get("quota_group")), "Provider requires quota_group for shared account quotas")
            integer(item.get("requests_per_day"), 1, 1000000, "provider daily quota")
            integer(item.get("requests_per_minute"), 1, 100000, "provider minute quota")
            self.providers[item["id"]] = item
        self.models = {}
        for item in raw.get("models", []):
            require(identifier(item.get("id")) and item["id"] not in self.models, "Duplicate/invalid route")
            require(item.get("provider") in self.providers, "Unknown provider")
            require(identifier(item.get("model")) and identifier(item.get("family")), "Model and family required")
            integer(item.get("context_tokens"), 1024, 2000000, "context size")
            integer(item.get("max_output_tokens"), 1, 200000, "output size")
            require(isinstance(item.get("capabilities"), list) and set(item["capabilities"]) <= {"text", "json", "tools", "stream"}, "Invalid capabilities")
            policy = item.get("free", {})
            require(policy.get("status") in {"unknown", "free", "paid", "expired"}, "Invalid free status")
            if policy.get("status") == "free":
                checked, until = utc(policy.get("checked_at")), utc(policy.get("verify_until"))
                require(checked < until <= checked + timedelta(days=7), "Free evidence must expire within 7 days")
                require(policy.get("kind") in {"ongoing", "quota", "promotion"}, "Free kind required")
                require(isinstance(policy.get("evidence"), list) and bool(policy["evidence"]), "Official evidence required")
                for url in policy["evidence"]:
                    endpoint(url)
                if policy.get("kind") == "promotion":
                    utc(policy.get("expires_at"))
                elif policy.get("expires_at"):
                    utc(policy["expires_at"])
                require(policy.get("billing_guard") in {"free_only_endpoint", "billing_disabled", "hard_zero_cap"}, "Provider-side billing guard required")
                require(policy.get("account_verified") is True, "Account free eligibility must be verified")
            self.models[item["id"]] = item
        self.aliases = raw.get("aliases", {})
        require(isinstance(self.aliases, dict), "aliases must be an object")
        for name, routes in self.aliases.items():
            require(identifier(name) and name not in self.models, "Alias conflicts with model")
            require(isinstance(routes, list) and len(routes) == len(set(routes)), "Alias must contain unique route IDs")
            require(all(route in self.models for route in routes), "Unknown route in alias")
        for client in self.clients.values():
            require(all(x in self.models or x in self.aliases for x in client["allowed_models"]), "Unknown allowed client model")

    @classmethod
    def load(cls, path, **kwargs):
        try:
            return cls(json.loads(Path(path).read_text()), **kwargs)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConfigError("Malformed router configuration") from exc

    def eligible(self, model, now=None):
        now = now or datetime.now(timezone.utc)
        policy = model["free"]
        if model.get("enabled") is not True or policy.get("status") != "free":
            return False
        if not utc(policy["checked_at"]) <= now < utc(policy["verify_until"]):
            return False
        if policy.get("expires_at") and now >= utc(policy["expires_at"]):
            return False
        provider = self.providers[model["provider"]]
        return bool(self.env.get(provider["key_env"]))
