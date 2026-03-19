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

from dotenv import load_dotenv
load_dotenv()

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

    # Inject live data by replacing the __DASHBOARD_DATA__ placeholder in the template
    data_json = json.dumps(data, indent=2, default=str)
    html = html.replace("__DASHBOARD_DATA__", data_json, 1)

    # Inject auto-refresh meta tag
    refresh_tag = f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">\n'
    html = html.replace("</head>", f"{refresh_tag}</head>", 1)
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
        elif self.path == "/costs":
            self._handle_costs()
        elif self.path == "/scheduler":
            self._handle_scheduler()
        else:
            self.send_error(404, "Not found")

    def _serve_dashboard(self):
        try:
            html = _render_dashboard(self.store)
            # Inject nav bar into the dashboard page
            nav_html = (
                '<nav style="margin-bottom:20px;font-family:monospace">'
                '<a href="/" style="color:#e94560;margin-right:15px;text-decoration:none">Dashboard</a>'
                '<a href="/costs" style="color:#e94560;margin-right:15px;text-decoration:none">Costs</a>'
                '<a href="/scheduler" style="color:#e94560;margin-right:15px;text-decoration:none">Scheduler</a>'
                '<a href="/data" style="color:#e94560;margin-right:15px;text-decoration:none">API</a>'
                '</nav>'
            )
            html = html.replace("<body>", f"<body>{nav_html}", 1)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            logger.exception("Dashboard render error")
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_costs(self):
        try:
            from src.swe_team.token_tracker import TokenTracker
            tracker = TokenTracker()
            summary = tracker.summary()

            rows = ""
            for model, data in summary.get("by_model", {}).items():
                rows += (
                    f"<tr><td>{model}</td><td>{data['calls']}</td>"
                    f"<td>{data['input_tokens']:,}</td>"
                    f"<td>{data['output_tokens']:,}</td>"
                    f"<td>${data['cost_usd']:.4f}</td></tr>"
                )

            html = f"""<!DOCTYPE html>
<html><head><title>SWE-Squad Costs</title>
<meta http-equiv="refresh" content="60">
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
.cards {{ display: flex; gap: 20px; margin-bottom: 20px; }}
.card {{ background: #16213e; padding: 20px; border-radius: 8px; min-width: 200px; }}
.card h3 {{ margin: 0; color: #0f3460; font-size: 14px; }}
.card .value {{ font-size: 32px; color: #e94560; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; }}
th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #0f3460; }}
th {{ color: #e94560; }}
nav {{ margin-bottom: 20px; }}
nav a {{ color: #e94560; margin-right: 15px; text-decoration: none; }}
</style></head><body>
<nav><a href="/">Dashboard</a> <a href="/costs">Costs</a> <a href="/scheduler">Scheduler</a> <a href="/data">API</a></nav>
<h1>Token Usage &amp; Costs</h1>
<div class="cards">
<div class="card"><h3>Total Cost</h3><div class="value">${summary['total_cost_usd']:.4f}</div></div>
<div class="card"><h3>Today's Spend</h3><div class="value">${summary['daily_spend']:.4f}</div></div>
<div class="card"><h3>Total Calls</h3><div class="value">{summary['total_records']}</div></div>
</div>
<h2>By Model</h2>
<table><tr><th>Model</th><th>Calls</th><th>Input Tokens</th><th>Output Tokens</th><th>Cost</th></tr>
{rows}</table>
</body></html>"""

            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            logger.exception("Costs page error")
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_scheduler(self):
        try:
            from src.swe_team.scheduler import JobStore
            store = JobStore(PROJECT_ROOT / "data" / "swe_team" / "jobs.json")
            jobs = store.load_all()

            rows = ""
            for j in jobs:
                status_color = {
                    "scheduled": "#4CAF50",
                    "running": "#FF9800",
                    "failed": "#f44336",
                    "paused": "#9E9E9E",
                }.get(j.status.value, "#e0e0e0")
                next_run = j.next_run[:16] if j.next_run else "\u2014"
                rows += (
                    f'<tr><td>{j.job_id}</td><td>{j.name}</td>'
                    f'<td>{j.schedule_type.value}</td>'
                    f'<td>{j.priority.value}</td>'
                    f'<td style="color:{status_color}">{j.status.value.upper()}</td>'
                    f'<td>{next_run}</td><td>{j.run_count}</td></tr>'
                )

            html = f"""<!DOCTYPE html>
<html><head><title>SWE-Squad Scheduler</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; }}
th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #0f3460; }}
th {{ color: #e94560; }}
nav {{ margin-bottom: 20px; }}
nav a {{ color: #e94560; margin-right: 15px; text-decoration: none; }}
</style></head><body>
<nav><a href="/">Dashboard</a> <a href="/costs">Costs</a> <a href="/scheduler">Scheduler</a> <a href="/data">API</a></nav>
<h1>Job Scheduler</h1>
<p>{len(jobs)} jobs configured</p>
<table><tr><th>ID</th><th>Name</th><th>Type</th><th>Priority</th><th>Status</th><th>Next Run</th><th>Runs</th></tr>
{rows}</table>
</body></html>"""

            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            logger.exception("Scheduler page error")
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _serve_json(self):
        try:
            from scripts.ops.dashboard_data import generate_dashboard_data
            data = generate_dashboard_data(self.store)
            self._json_response(data)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json_response(self, data: dict):
        try:
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")


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
