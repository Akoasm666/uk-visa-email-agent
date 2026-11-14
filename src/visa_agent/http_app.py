"""Minimal stdlib HTTP app for S1.1 when FastAPI cannot be installed."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from visa_agent.config import Settings, get_settings
from visa_agent.storage import LocalFileStore, check_database


def build_health(settings: Settings) -> dict[str, Any]:
    store = LocalFileStore(settings.file_storage_dir)
    db = check_database(settings.database_url)
    return {
        "success": True,
        "data": {
            "status": "ok" if db.ok else "degraded",
            "database": {"ok": db.ok, "via": db.via, "detail": db.detail},
            "file_storage": {
                "ok": store.exists(),
                "path": str(settings.file_storage_dir),
            },
            "email": {
                "configured": settings.email_configured,
                "protocol": "smtp",
                "from_addr": settings.email_from or None,
                "allowlist_count": len(settings.outbound_allowlist),
                "imap_configured": settings.imap_configured,
            },
            "model": {
                "configured": settings.model_configured,
                "provider": "siliconflow",
                "model": settings.siliconflow_model,
            },
        },
    }


class AppHandler(BaseHTTPRequestHandler):
    settings: Settings

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep demo quiet; tests assert via return bodies.
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, build_health(self.settings))
            return
        if path == "/":
            self._send_json(
                200,
                {
                    "success": True,
                    "data": {
                        "service": "visa-email-agent",
                        "stage": "S1.1",
                        "docs": "/health",
                    },
                },
            )
            return
        self._send_json(404, {"success": False, "error": "NOT_FOUND"})


def make_server(settings: Settings | None = None) -> ThreadingHTTPServer:
    cfg = settings or get_settings()
    cfg.file_storage_dir.mkdir(parents=True, exist_ok=True)
    handler = type("BoundHandler", (AppHandler,), {"settings": cfg})
    return ThreadingHTTPServer((cfg.app_host, cfg.app_port), handler)


def main() -> None:
    settings = get_settings()
    server = make_server(settings)
    print(
        f"visa-email-agent S1.1 listening on http://{settings.app_host}:{settings.app_port}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
