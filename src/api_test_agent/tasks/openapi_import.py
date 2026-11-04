"""Fetch one explicitly selected OpenAPI document; never crawl references."""

from urllib.parse import parse_qsl, urlsplit

import httpx

MAX_DOCUMENT_BYTES = 1_000_000


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


def fetch_openapi_document(url: str) -> str:
    validate_openapi_url(url)
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"OpenAPI download returned HTTP {response.status_code}")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_DOCUMENT_BYTES:
                        raise ValueError("OpenAPI document exceeds snapshot limit")
    except httpx.HTTPError:
        # Do not echo response bodies or request headers into CLI/logs.
        raise ValueError("OpenAPI download failed (network or timeout)") from None
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("OpenAPI document must be UTF-8") from None
