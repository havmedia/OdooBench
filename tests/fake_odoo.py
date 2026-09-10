"""A stand-in Odoo that speaks just enough JSON-RPC to be measured.

It records every payload it receives, which is how the tests check that OdooBench
sends what the real web client sends rather than something of its own invention.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional


class FakeOdoo:
    def __init__(
        self,
        password: str = "secret",
        fail_every: int = 0,
        document: bytes = b"%PDF-1.4 pretend",
    ) -> None:
        self.password = password
        self.fail_every = fail_every
        self.document = document
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._count = 0
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return "http://127.0.0.1:%d" % port

    def record(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._count += 1
            count = self._count
            self.calls.append({"path": path, "params": params})

        if path == "/web/session/authenticate":
            if params.get("password") != self.password:
                return {"result": False}
            return {"result": {"uid": 2, "username": params.get("login")}}

        if self.fail_every and count % self.fail_every == 0:
            return {"error": {"message": "server is busy", "code": 500}}

        method = params.get("method")
        if method == "fields_get":
            return {
                "result": {
                    "id": {"type": "integer", "store": True},
                    "create_date": {"type": "datetime", "store": True},
                    "date": {"type": "date", "store": True},
                    "commitment_date": {"type": "datetime", "store": True},
                    "partner_id": {"type": "many2one", "store": True},
                    "team_id": {"type": "many2one", "store": True},
                    "currency_id": {"type": "many2one", "store": True},
                    "price_total": {"type": "monetary", "store": True},
                    "margin": {"type": "float", "store": False},
                    "name": {"type": "char", "store": True},
                }
            }
        if params.get("model") == "ir.actions.report" and method == "search_read":
            return {
                "result": [
                    {
                        "id": 7,
                        "report_name": "sale.report_saleorder",
                        "model": "sale.order",
                        "report_type": "qweb-pdf",
                        "name": "Quotation",
                    }
                ]
            }
        if method == "search_count":
            return {"result": 4711}
        if method == "read_group":
            return {"result": [{"__count": 3, "id_count": 3}]}
        return {"result": [{"id": index} for index in range(3)]}

    def __enter__(self) -> "FakeOdoo":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                payload = outer.record(self.path, body.get("params", {}))
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                with outer._lock:
                    outer.calls.append({"path": self.path, "params": {}})
                body = outer.document
                self.send_response(200 if body else 500)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    def report_paths(self) -> List[str]:
        return [call["path"] for call in self.calls if call["path"].startswith("/report/")]

    def call_kw_payloads(self) -> List[Dict[str, Any]]:
        return [call["params"] for call in self.calls if call["path"] == "/web/dataset/call_kw"]
