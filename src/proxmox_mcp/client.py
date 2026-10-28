"""A small Proxmox VE API client (api2/json, any Proxmox VE 8 or 9): an API token in the Authorization header, the
reads the tools need, and the writes that start a task. Proxmox answers a write with a task id (UPID); `wait`
follows the task to its end. Errors come back as an HTTP reason ("Permission check failed ...") or as
{"errors": {...}}; `call` turns both into ProxmoxError.
"""

import os
import time
from typing import Any

import httpx


class ProxmoxError(Exception):
    pass


class Proxmox:
    def __init__(self, url: str | None = None, token_id: str | None = None, secret: str | None = None,
                 verify: bool | None = None, transport: httpx.BaseTransport | None = None):
        self.url = (url or os.environ.get("PVE_URL", "https://127.0.0.1:8006")).rstrip("/")
        token_id = token_id or os.environ.get("PVE_TOKEN_ID", "")
        secret = secret or os.environ.get("PVE_TOKEN_SECRET", "")
        if not (token_id and secret):
            raise ProxmoxError("PVE_TOKEN_ID and PVE_TOKEN_SECRET are not set (scripts/up.sh writes them to .env)")
        if verify is None:  # the local demo's certificate is self-signed; a real installation keeps this on
            verify = os.environ.get("PVE_VERIFY_TLS", "1") != "0"
        self.http = httpx.Client(base_url=self.url + "/api2/json/", timeout=30, verify=verify, transport=transport,
                                 headers={"Authorization": f"PVEAPIToken={token_id}={secret}"})
        self._node: str | None = None

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path.lstrip("/"), **kw)
        if r.status_code >= 400:
            body = r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else {}
            errors = body.get("errors") if isinstance(body, dict) else None
            detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) if isinstance(errors, dict) else ""
            raise ProxmoxError(detail or (body.get("message") if isinstance(body, dict) else "") or
                               r.reason_phrase or f"Proxmox answered {r.status_code}")
        return r.json().get("data")

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, data: dict | None = None) -> Any:
        return self.call("POST", path, data={k: v for k, v in (data or {}).items() if v is not None})

    def node(self) -> str:
        """The node to read storage and tasks from (the demo has one; a cluster's guests carry their own node)."""
        if self._node is None:
            nodes = self.get("nodes") or []
            if not nodes:
                raise ProxmoxError("Proxmox reports no nodes")
            self._node = min(n["node"] for n in nodes)
        return self._node

    def wait(self, node: str, upid: str, timeout_s: float = 90) -> str:
        """Follow a task until it ends; returns its exit status ("OK" when it worked)."""
        end = time.monotonic() + timeout_s
        while True:
            st = self.get(f"nodes/{node}/tasks/{upid}/status")
            if st.get("status") == "stopped":
                return st.get("exitstatus") or "unknown"
            if time.monotonic() > end:
                return "still running"
            time.sleep(1)
