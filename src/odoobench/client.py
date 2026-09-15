"""The calls Odoo's own web client makes, on top of whatever HTTP client it is given.

Under load the client is Locust's, so every call lands in Locust's statistics
under the name the operation chose. For the small lookups OdooBench does before a
test starts, it is a plain `requests` session. Both speak the same methods, so
the workload does not know or care which one it is holding.

The one thing this module exists to get right: **Odoo reports its errors inside a
200 response.** Left alone, Locust would count a server that answers every
request with "internal error" as a server answering every request. Every call
therefore inspects the body and marks the request failed itself.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests


class OdooError(RuntimeError):
    """An error Odoo reported inside a 200 response, or a transport failure."""


def _is_locust(http: Any) -> bool:
    return http.__class__.__name__ in {"HttpSession", "FastHttpSession"}


class OdooClient:
    """One logged-in user."""

    def __init__(
        self,
        http: Any,
        url: str,
        db: str,
        login: str,
        password: str,
        timeout: int = 300,
    ) -> None:
        self.http = http
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.password = password
        self.timeout = timeout
        self.uid: Optional[int] = None
        self.timed = _is_locust(http)

    @classmethod
    def standalone(
        cls, url: str, db: str, login: str, password: str, timeout: int = 300
    ) -> "OdooClient":
        """A client for the lookups that happen before any load is generated."""
        return cls(requests.Session(), url, db, login, password, timeout)

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, params: Dict[str, Any], name: str) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params})
        headers = {"Content-Type": "application/json"}

        if not self.timed:
            try:
                response = self.http.post(
                    self.url + path, data=body, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise OdooError("%s on %s" % (exc, path)) from exc
            return _unwrap(response, path)

        with self.http.post(
            self.url + path,
            data=body,
            headers=headers,
            timeout=self.timeout,
            name=name,
            catch_response=True,
        ) as response:
            try:
                result = _unwrap(response, path)
            except OdooError as exc:
                response.failure(str(exc))
                raise
            response.success()
            return result

    def _get(self, path: str, name: str) -> bytes:
        if not self.timed:
            try:
                response = self.http.get(self.url + path, timeout=self.timeout)
            except requests.RequestException as exc:
                raise OdooError("%s on %s" % (exc, path)) from exc
            return _unwrap_document(response, path)

        with self.http.get(
            self.url + path, timeout=self.timeout, name=name, catch_response=True
        ) as response:
            try:
                content = _unwrap_document(response, path)
            except OdooError as exc:
                response.failure(str(exc))
                raise
            response.success()
            return content

    # -- what a session does ----------------------------------------------

    def authenticate(self, name: str = "login") -> int:
        result = self._post(
            "/web/session/authenticate",
            {"db": self.db, "login": self.login, "password": self.password},
            name,
        )
        if not result or not result.get("uid"):
            raise OdooError("authentication failed for %r on %r" % (self.login, self.db))
        self.uid = int(result["uid"])
        return self.uid

    def call_kw(
        self,
        model: str,
        method: str,
        args: List[Any],
        kwargs: Optional[Dict[str, Any]] = None,
        name: str = "",
    ) -> Any:
        return self._post(
            "/web/dataset/call_kw",
            {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs if kwargs is not None else {"context": {}},
            },
            name or method,
        )

    def search_read(
        self,
        model: str,
        domain: List[Any],
        fields: List[str],
        limit: int = 80,
        offset: int = 0,
        order: str = "",
        name: str = "",
    ) -> Any:
        return self.call_kw(
            model,
            "search_read",
            [domain, fields],
            {"limit": limit, "offset": offset, "order": order, "context": {}},
            name=name or "search_read",
        )

    def search_count(self, model: str, domain: List[Any], name: str = "") -> Any:
        return self.call_kw(
            model, "search_count", [domain], {"context": {}}, name=name or "search_count"
        )

    def read_group(
        self,
        model: str,
        domain: List[Any],
        fields: List[str],
        groupby: List[str],
        lazy: bool = True,
        limit: Optional[int] = None,
        name: str = "",
    ) -> Any:
        kwargs: Dict[str, Any] = {"lazy": lazy, "context": {}}
        if limit:
            kwargs["limit"] = limit
        return self.call_kw(
            model, "read_group", [domain, fields, groupby], kwargs, name=name or "read_group"
        )

    def fetch(self, path: str, name: str = "") -> bytes:
        """A plain GET, for the routes that return a file rather than JSON.

        Printed documents do not come back over JSON-RPC. The web client asks for
        them at `/report/<converter>/<name>/<ids>` and gets a document.
        """
        return self._get(path, name or "document")


def _unwrap(response: Any, path: str) -> Any:
    if response.status_code != 200:
        raise OdooError("HTTP %s on %s" % (response.status_code, path))
    try:
        body = response.json()
    except ValueError as exc:
        raise OdooError("not JSON from %s" % path) from exc
    if "error" in body:
        raise OdooError(json.dumps(body["error"])[:200])
    return body.get("result")


def _unwrap_document(response: Any, path: str) -> bytes:
    if response.status_code != 200:
        raise OdooError("HTTP %s on %s" % (response.status_code, path))
    if not response.content:
        raise OdooError("empty document from %s" % path)
    return response.content
