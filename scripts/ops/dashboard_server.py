#!/usr/bin/env python3
"""
SWE-Squad Live Dashboard Server

Serves the dashboard HTML at http://0.0.0.0:PORT/ with auto-refresh every 60s.
Generates fresh data on each request — no caching layer needed.

Usage:
    python3 scripts/ops/dashboard_server.py [--port 8080] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.config import load_config
from src.swe_team.ticket_store import TicketStore

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = PROJECT_ROOT / "templates"
_DEFAULT_PORT = 8080
_REFRESH_SECONDS = 60


def _load_store(config):
    """Load ticket store — Supabase if configured, else local JSON."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if supabase_url and supabase_key:
        try:
            from src.swe_team.supabase_store import SupabaseTicketStore
            return SupabaseTicketStore(
                supabase_url=supabase_url,
                supabase_key=supabase_key,
                team_id=config.team_id,
            )
        except Exception as exc:
            logger.warning("Supabase unavailable, falling back to local store: %s", exc)
    data_dir = PROJECT_ROOT / "data" / "swe_team"
    data_dir.mkdir(parents=True, exist_ok=True)
    return TicketStore(path=data_dir / "tickets.json")


def _render_dashboard(store) -> str:
    """Generate dashboard HTML with embedded fresh data."""
    from scripts.ops.dashboard_data import generate_dashboard_data

    data = generate_dashboard_data(store)
    template_path = _TEMPLATES_DIR / "dashboard.html"

    if not template_path.exists():
        return f"<pre>Template not found: {template_path}</pre>"

    html = template_path.read_text(encoding="utf-8")

    # Inject live data as a JS variable and auto-refresh meta tag
    data_json = json.dumps(data, indent=2, default=str)
    inject = (
        f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">\n'
        f"<script>window.__SWE_DATA__ = {data_json};</script>\n"
    )
    html = html.replace("</head>", f"{inject}</head>", 1)
    return html


class DashboardHandler(BaseHTTPRequestHandler):
    store = None  # set at startup

    def log_message(self, fmt, *args):  # suppress default access log noise
        logger.debug("HTTP %s %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path in ("/", "/dashboard"):
            self._serve_dashboard()
        elif self.path == "/health":
            self._json_response({"status": "ok"})
        elif self.path == "/data":
            self._serve_json()
        else:
            self.send_error(404, "Not found")

    def _serve_dashboard(self):
        try:
            html = _render_dashboard(self.store)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            logger.exception("Dashboard render error")
            self.send_error(500, str(exc))

    def _serve_json(self):
        try:
            from scripts.ops.dashboard_data import generate_dashboard_data
            data = generate_dashboard_data(self.store)
            self._json_response(data)
        except Exception as exc:
            self.send_error(500, str(exc))

    def _json_response(self, data: dict):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="SWE-Squad live dashboard server")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    config = load_config()
    store = _load_store(config)

    DashboardHandler.store = store

    server = HTTPServer((args.host, args.port), DashboardHandler)
    logger.info("Dashboard running at http://%s:%d/", args.host, args.port)
    logger.info("Auto-refresh: every %ds | Data API: /data | Health: /health", _REFRESH_SECONDS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server")


if __name__ == "__main__":
    main()
