"""The calls Odoo's own web client makes, and nothing else.

A benchmark that talks to Odoo through some side door measures the side door.
OdooBench authenticates the way a browser does, keeps the session cookie, and then
sends `call_kw` payloads that are byte-for-byte the shape the web client sends
for a list view. Standard library only, so this runs on a server where nothing
may be installed.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class RpcError(RuntimeError):
    """An error Odoo reported inside a 200 response, or a transport failure."""


class Session:
    """One logged-in user. Not thread-safe: give every worker its own."""

    def __init__(
        self,
        url: str,
        db: str,
        login: str,
        password: str,
        timeout: int = 300,
    ) -> None:
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.password = password
        self.timeout = timeout
        self.uid: Optional[int] = None
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    def authenticate(self) -> int:
        result = self.call(
            "/web/session/authenticate",
            {"db": self.db, "login": self.login, "password": self.password},
        )
        if not result or not result.get("uid"):
            raise RpcError("authentication failed for %r on %r" % (self.login, self.db))
        self.uid = int(result["uid"])
        return self.uid

    def call(self, path: str, params: Dict[str, Any]) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}).encode()
        request = urllib.request.Request(
            self.url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RpcError("HTTP %s on %s" % (exc.code, path)) from exc
        except Exception as exc:  # noqa: BLE001 - transport failures are results too
            raise RpcError("%s on %s" % (exc, path)) from exc

        if "error" in payload:
            raise RpcError(_short(payload["error"]))
        return payload.get("result")

    def fetch(self, path: str) -> bytes:
        """A plain GET with the session cookie, for the routes that return files.

        Reports do not come back over JSON-RPC. The web client asks for them at
        `/report/<converter>/<name>/<ids>` and gets a document, which is why this
        is the one place OdooBench leaves call_kw behind.
        """
        request = urllib.request.Request(self.url + path, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise RpcError("HTTP %s on %s" % (exc.code, path)) from exc
        except Exception as exc:  # noqa: BLE001 - transport failures are results too
            raise RpcError("%s on %s" % (exc, path)) from exc

        if not body:
            raise RpcError("empty document from %s" % path)
        return body

    def call_kw(
        self,
        model: str,
        method: str,
        args: List[Any],
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self.call(
            "/web/dataset/call_kw",
            {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs if kwargs is not None else {"context": {}},
            },
        )

    # -- the three calls a list view actually makes -----------------------

    def search_read(
        self,
        model: str,
        domain: List[Any],
        fields: List[str],
        limit: int = 80,
        offset: int = 0,
        order: str = "",
    ) -> Any:
        return self.call_kw(
            model,
            "search_read",
            [domain, fields],
            {"limit": limit, "offset": offset, "order": order, "context": {}},
        )

    def search_count(self, model: str, domain: List[Any]) -> Any:
        return self.call_kw(model, "search_count", [domain], {"context": {}})

    def read_group(
        self,
        model: str,
        domain: List[Any],
        fields: List[str],
        groupby: List[str],
    ) -> Any:
        return self.call_kw(
            model,
            "read_group",
            [domain, fields, groupby],
            {"lazy": True, "context": {}},
        )


def _short(error: Any, limit: int = 200) -> str:
    text = json.dumps(error) if not isinstance(error, str) else error
    return text[:limit]
