"""Shared pieces of the credential gateway: the refusal exception, the service protocol and the upstream client."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from http.client import HTTPResponse
from typing import Any, Protocol

TIMEOUT_SECONDS = 60


class Denied(Exception):
    """A request the policy refuses; carries the HTTP status, a reason and optionally a protocol-specific reply."""

    def __init__(self, status: int, reason: str, reply: tuple[str, bytes] | None = None) -> None:
        """Store status and reason; `reply` (content type, body) replaces the default JSON error body."""
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.reply = reply


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand redirects back as responses; following one would resend the credentials to the redirect target."""

    def redirect_request(self, *args: Any) -> None:
        return None


class Upstream:
    """HTTPS client for one upstream host; adds the service's credentials and ignores proxy variables."""

    def __init__(self, base_url: str, context: ssl.SSLContext, auth_headers: Callable[[str], dict[str, str]]) -> None:
        """Store the base URL (without trailing slash), the TLS context and the credential function (path -> headers)."""
        self._base_url = base_url.rstrip('/')
        self._auth_headers = auth_headers
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect()
        )

    def open(
        self, method: str, path_query: str, body: bytes | None, headers: Mapping[str, str]
    ) -> HTTPResponse | urllib.error.HTTPError:
        """Send one request with the credentials; return the response, also for HTTP error statuses (caller closes)."""
        all_headers = {**headers, **self._auth_headers(path_query)}
        request = urllib.request.Request(self._base_url + path_query, data=body, method=method, headers=all_headers)
        try:
            return self._opener.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            return error

    def get_json(self, path_query: str) -> tuple[int, Any]:
        """GET a JSON resource; return the status and the decoded body (None when it is no JSON)."""
        with self.open('GET', path_query, None, {'Accept': 'application/json'}) as response:
            status = response.status
            raw = response.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None


class Service(Protocol):
    """One upstream host behind the gateway with its rules."""

    host: str

    def authorize(
        self, method: str, path: str, query: str, headers: Mapping[str, str], body: bytes | None, upstream: Upstream
    ) -> str:
        """Check a request; return the query string to forward.

        Raises:
            Denied: when the rules refuse the request.
        """
        ...

    def response_observer(self, method: str, path: str) -> Callable[[bytes], None] | None:
        """Return a callback that wants the body of a successful response to this request, or None."""
        ...
