"""Bounded OpenAPI downloads; credentials stay in process environment."""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_DOCUMENT_BYTES = 20_000_000


def validate_openapi_url(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("invalid OpenAPI URL") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.fragment
        or (port is not None and port == 0)
        or any(ord(c) < 33 for c in value)
        or any(
            key.lower() in {"token", "access_token", "api_key", "apikey", "password", "secret"}
            for key, _ in parse_qsl(url.query)
        )
    ):
        raise ValueError("OpenAPI URL must be HTTP(S), without credentials or fragment")
    return value


def origin(url: str) -> str:
    parsed = urlsplit(validate_openapi_url(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def snapshot_path(url: str) -> str:
    return f"url-import/{hashlib.sha256(url.encode()).hexdigest()}.json"


class OpenAPIImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_document_bytes: int = Field(default=MAX_DOCUMENT_BYTES, ge=1)
    max_total_bytes: int = Field(default=100_000_000, ge=1)
    max_documents: int = Field(default=64, ge=1, le=1024)
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    total_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_redirects: int = Field(default=5, ge=0, le=20)
    fetch_refs: bool = True
    allowed_origins: tuple[str, ...] = ()
    header_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parsed = urlsplit(validate_openapi_url(value))
            if parsed.path not in {"", "/"} or parsed.query:
                raise ValueError("allowed_origins must contain origins, not document paths")
        return tuple(origin(value) for value in values)

    @field_validator("header_env")
    @classmethod
    def validate_headers(cls, values: dict[str, str]) -> dict[str, str]:
        for name, env_name in values.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise ValueError("invalid authentication header name")
            if name.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
                raise ValueError("transport headers cannot be configured")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
                raise ValueError("header_env values must be environment variable names")
        return values


@dataclass
class DocumentBundle:
    snapshots: dict[str, str] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Downloader:
    def __init__(self, url: str, options: OpenAPIImportOptions):
        self.options = options
        self.initial_origin = origin(url)
        self.allowed = {self.initial_origin, *options.allowed_origins}
        self.headers = {}
        for name, env_name in options.header_env.items():
            value = os.environ.get(env_name)
            if not value:
                raise ValueError(f"Missing document credential environment variable: {env_name}")
            if "\r" in value or "\n" in value:
                raise ValueError("Document credential contains invalid header characters")
            self.headers[name] = value
        self.total = 0
        self.deadline = time.monotonic() + options.total_timeout_seconds

    def fetch(self, client: httpx.Client, url: str) -> tuple[str, str]:
        current = validate_openapi_url(url)
        for hop in range(self.options.max_redirects + 1):
            if origin(current) not in self.allowed:
                raise ValueError("Document origin not allowed; configure allowed_origins")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Document import exceeded total_timeout_seconds")
            # Never propagate credentials or server cookies to another origin.
            headers = {"Accept": "application/json, application/yaml"}
            if origin(current) == self.initial_origin:
                headers.update(self.headers)
            client.cookies.clear()
            with client.stream(
                "GET",
                current,
                headers=headers,
                timeout=min(self.options.timeout_seconds, remaining),
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == self.options.max_redirects:
                        raise ValueError("Document redirect limit reached; check max_redirects")
                    target = urljoin(current, location)
                    validate_openapi_url(target)
                    if urlsplit(current).scheme == "https" and urlsplit(target).scheme != "https":
                        raise ValueError("HTTPS document redirect cannot downgrade to HTTP")
                    current = target
                    continue
                if response.status_code != 200:
                    hint = (
                        "; configure header_env for authentication"
                        if response.status_code in {401, 403}
                        else ""
                    )
                    raise ValueError(f"OpenAPI download returned HTTP {response.status_code}{hint}")
                payload = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if time.monotonic() > self.deadline:
                        raise ValueError("Document import exceeded total_timeout_seconds")
                    self.total += len(chunk)
                    if len(payload) + len(chunk) > self.options.max_document_bytes:
                        raise ValueError(
                            "OpenAPI document exceeds snapshot limit; increase "
                            "max_document_bytes in --openapi-config (document not truncated)"
                        )
                    if self.total > self.options.max_total_bytes:
                        raise ValueError("Document import exceeds max_total_bytes")
                    payload.extend(chunk)
                try:
                    return payload.decode("utf-8-sig"), current
                except UnicodeDecodeError:
                    raise ValueError("OpenAPI document must be UTF-8") from None
        raise ValueError("Document redirect limit reached")


def fetch_openapi_document(url: str, options: OpenAPIImportOptions | None = None) -> str:
    """Fetch one document, retained for callers that do not need dependency collection."""
    options = options or OpenAPIImportOptions(max_document_bytes=MAX_DOCUMENT_BYTES)
    downloader = Downloader(url, options)
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            return downloader.fetch(client, url)[0]
    except httpx.HTTPError:
        raise ValueError("OpenAPI download failed (network or timeout)") from None


def fetch_openapi_bundle(
    url: str,
    options: OpenAPIImportOptions | None = None,
) -> DocumentBundle:
    """Fetch a spec or Swagger/Knife4j resource list plus selected dependencies.

    Original response text is preserved. Resolution later uses only these frozen files.
    A failed dependency is retained as unresolved; failure of an explicitly selected
    root or aggregation member aborts discovery instead of publishing a partial API set.
    """
    from api_test_agent.adapters.openapi import StrictLoader

    options = options or OpenAPIImportOptions()
    downloader = Downloader(url, options)
    bundle = DocumentBundle()
    queue = [(url, True)]
    visited: set[str] = set()
    specs = 0
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            while queue:
                requested, required = queue.pop(0)
                if requested in visited:
                    continue
                if len(visited) >= options.max_documents:
                    raise ValueError("Document import exceeds max_documents")
                visited.add(requested)
                try:
                    content, final_url = downloader.fetch(client, requested)
                    data = yaml.load(content, Loader=StrictLoader)
                    if not isinstance(data, (dict, list)):
                        raise ValueError("URL response is not an OpenAPI/Swagger document")
                except (ValueError, httpx.HTTPError, yaml.YAMLError) as exc:
                    # Never expose payloads, credential values or unsafe URLs in errors.
                    if required:
                        if isinstance(exc, ValueError) and not isinstance(exc, yaml.YAMLError):
                            raise
                        raise ValueError(
                            "OpenAPI download failed (network, timeout or syntax)"
                        ) from None
                    bundle.warnings.append(
                        f"{snapshot_path(requested)}: remote reference unavailable; unresolved"
                    )
                    continue
                path = snapshot_path(requested)
                bundle.snapshots[path] = content
                bundle.urls[path] = final_url
                is_spec = isinstance(data, dict) and ("openapi" in data or "swagger" in data)
                if is_spec:
                    specs += 1
                elif required:
                    # Springfox /swagger-resources and OpenAPI swagger-config urls.
                    members = data if isinstance(data, list) else data.get("urls")
                    if not isinstance(members, list) or not members:
                        raise ValueError(
                            "URL response is not an OpenAPI/Swagger document or resource list"
                        )
                    for member in members:
                        link = (
                            member.get("url") or member.get("location")
                            if isinstance(member, dict)
                            else None
                        )
                        if not isinstance(link, str) or not link:
                            raise ValueError("Invalid Swagger resource list member")
                        queue.append((validate_openapi_url(urljoin(final_url, link)), True))
                if not options.fetch_refs:
                    continue
                stack = [data]
                seen_nodes: set[int] = set()
                while stack:
                    node = stack.pop()
                    if not isinstance(node, (dict, list)) or id(node) in seen_nodes:
                        continue
                    seen_nodes.add(id(node))
                    if isinstance(node, list):
                        stack.extend(node)
                        continue
                    reference = node.get("$ref")
                    if isinstance(reference, str) and not reference.startswith("#"):
                        target, _ = urldefrag(urljoin(final_url, reference))
                        try:
                            validate_openapi_url(target)
                            allowed = origin(target) in downloader.allowed
                        except ValueError:
                            allowed = False
                        if allowed:
                            queue.append((target, False))
                        else:
                            bundle.warnings.append(
                                f"{path}: remote reference origin not allowed; unresolved"
                            )
                    stack.extend(node.values())
    except httpx.HTTPError:
        raise ValueError("OpenAPI download failed (network or timeout)") from None
    if not specs:
        raise ValueError("No OpenAPI specifications found")
    return bundle
