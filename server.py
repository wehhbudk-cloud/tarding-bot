"""Read-only HTTP health endpoint for the paper bot."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from engine import BotEngine


def make_handler(engine: BotEngine) -> type[BaseHTTPRequestHandler]:
    class StatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/", "/healthz"}:
                self._send_json(404, {"error": "Not found."})
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "mode": "paper",
                    "trading_enabled": engine.is_enabled(),
                },
            )

        def do_POST(self) -> None:
            self._send_json(
                405,
                {"error": "This paper bot has no webhook or order endpoint."},
            )

        def log_message(self, format_string: str, *args: Any) -> None:
            logging.info("Status endpoint request from %s.", self.client_address[0])

        def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

    return StatusHandler


def serve(engine: BotEngine, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    server.daemon_threads = True
    logging.info(
        "Health server listening on %s:%s; TradingView webhooks are disabled.",
        host,
        port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Paper bot stopped.")
    finally:
        server.server_close()
