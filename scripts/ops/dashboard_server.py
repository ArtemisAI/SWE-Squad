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
import re
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
_JOBS_DIR = PROJECT_ROOT / "data" / "swe_team"


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


def _get_scheduler_and_store():
    """Get JobStore and JobScheduler instances for API handlers."""
    from src.swe_team.scheduler import JobStore, JobScheduler
    store = JobStore(_JOBS_DIR / "jobs.json")
    scheduler = JobScheduler(store=store)
    return store, scheduler


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
        elif self.path == "/api/activity":
            self._handle_api_activity()
        elif self.path == "/costs":
            self._handle_costs()
        elif self.path == "/scheduler":
            self._handle_scheduler()
        elif self.path == "/api/jobs":
            self._handle_list_jobs_api()
        elif re.match(r"^/api/jobs/[^/]+/history$", self.path):
            self._handle_job_history_api()
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        """Handle POST requests for scheduler CRUD operations."""
        path = self.path

        # POST /api/jobs — create a new job
        if path == "/api/jobs":
            self._handle_create_job()
            return

        # POST /api/jobs/<id>/<action>
        m = re.match(r"^/api/jobs/([^/]+)/(pause|resume|cancel|trigger|delete)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            self._handle_job_action(job_id, action)
            return

        self.send_error(404, "Not found")

    # --- Scheduler API helpers ---

    def _read_post_body(self) -> dict:
        """Read and parse JSON POST body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _handle_job_action(self, job_id: str, action: str):
        """Handle pause/resume/cancel/trigger/delete actions on a job."""
        try:
            _store, sched = _get_scheduler_and_store()
            if action == "delete":
                deleted = sched.delete_job(job_id)
                if not deleted:
                    self._json_response({"error": f"Job {job_id} not found"}, status=404)
                    return
                self._json_response({"ok": True, "deleted": job_id})
                return

            method = getattr(sched, f"{action}_job", None)
            if method is None:
                self._json_response({"error": f"Unknown action: {action}"}, status=400)
                return
            job = method(job_id)
            if job is None:
                self._json_response(
                    {"error": f"Job {job_id} not found or action not applicable"},
                    status=404,
                )
                return
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Job action %s/%s error", job_id, action)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_job(self):
        """Handle POST /api/jobs to create a new job."""
        try:
            from src.swe_team.scheduler import ScheduledJob
            body = self._read_post_body()
            if not body.get("name"):
                self._json_response({"error": "Job name is required"}, status=400)
                return
            job = ScheduledJob.from_dict(body)
            _store, sched = _get_scheduler_and_store()
            job = sched.add_job(job)
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Create job error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_jobs_api(self):
        """GET /api/jobs — return all jobs as JSON."""
        try:
            from src.swe_team.scheduler import JobStore
            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()
            self._json_response([j.to_dict() for j in jobs])
        except Exception as exc:
            logger.exception("List jobs API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_job_history_api(self):
        """GET /api/jobs/<id>/history — return run history for a job."""
        try:
            from src.swe_team.scheduler import RunHistoryStore
            m = re.match(r"^/api/jobs/([^/]+)/history$", self.path)
            if not m:
                self.send_error(404, "Not found")
                return
            job_id = m.group(1)
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            records = history_store.get_history(job_id=job_id, limit=50)
            self._json_response([r.to_dict() for r in records])
        except Exception as exc:
            logger.exception("Job history API error")
            self._json_response({"error": str(exc)}, status=500)

    # --- Page handlers ---

    def _serve_dashboard(self):
        try:
            html = _render_dashboard(self.store)
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

    def _handle_api_activity(self):
        log_path = PROJECT_ROOT / "logs" / "swe_team.log"
        entries = []
        if log_path.exists():
            lines = log_path.read_text().strip().split('\n')[-30:]
            for line in lines:
                if '[INFO]' in line and any(w in line for w in ['Investigating', 'attempt_fix', 'Triaged', 'SESSION', 'Dispatched', 'Claude CLI', 'gate:']):
                    parts = line.split(' ', 3)
                    entries.append({"time": parts[0] + ' ' + parts[1][:8] if len(parts) > 1 else "", "agent": "swe-squad", "action": parts[-1][:120] if parts else line[:120]})
        self._json_response(entries[-20:])

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

            from collections import defaultdict
            daily = defaultdict(float)
            if tracker._path.exists():
                for record in tracker._load_records():
                    day = record.timestamp[:10]
                    daily[day] += record.cost_usd

            daily_rows = ""
            for day in sorted(daily.keys())[-7:]:
                bar_width = min(int(daily[day] / max(max(daily.values(), default=1), 0.01) * 200), 200)
                daily_rows += f'<tr><td>{day}</td><td>${daily[day]:.4f}</td><td><div style="width:{bar_width}px;height:16px;background:#e94560;border-radius:4px;"></div></td></tr>'

            html = f"""<!DOCTYPE html>
<html><head><title>SWE-Squad Costs</title>
<meta http-equiv="refresh" content="60">
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
.cards {{ display: flex; gap: 20px; margin-bottom: 20px; }}
.card {{ background: #16213e; padding: 20px; border-radius: 8px; min-width: 200px; }}
.card h3 {{ margin: 0; color: #0f3460; font-size: 14px; }}
.card .value {{ font-size: 32px; color: #e94560; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; margin-bottom: 20px; }}
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
<h2>Daily Cost Trend (Last 7 Days)</h2>
<table><tr><th>Date</th><th>Cost</th><th>Trend</th></tr>
{daily_rows}</table>
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
            from src.swe_team.scheduler import JobStore, RunHistoryStore
            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()

            # Load recent run history for display
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            all_history = history_store.get_history(limit=100)

            rows = ""
            for j in jobs:
                status_color = {
                    "scheduled": "#4CAF50",
                    "running": "#FF9800",
                    "failed": "#f44336",
                    "paused": "#9E9E9E",
                    "completed": "#2196F3",
                    "cancelled": "#795548",
                }.get(j.status.value, "#e0e0e0")
                next_run = j.next_run[:16] if j.next_run else "---"
                last_run = j.last_run[:16] if j.last_run else "---"
                last_error = ""
                if j.last_error:
                    escaped = j.last_error.replace('"', '&quot;').replace('<', '&lt;')[:80]
                    last_error = f' <span title="{escaped}" style="color:#f44336;cursor:help">[err]</span>'

                jid = j.job_id
                actions = (
                    f'<button onclick="jobAction(\'{jid}\',\'trigger\')" title="Run now">Run</button> '
                    f'<button onclick="jobAction(\'{jid}\',\'pause\')" title="Pause scheduling">Pause</button> '
                    f'<button onclick="jobAction(\'{jid}\',\'resume\')" title="Resume scheduling">Resume</button> '
                    f'<button onclick="jobAction(\'{jid}\',\'cancel\')" title="Cancel job">Cancel</button> '
                    f'<button onclick="jobAction(\'{jid}\',\'delete\')" style="color:#f44336" title="Permanently delete">Del</button>'
                )
                rows += (
                    f'<tr><td><code>{j.job_id}</code></td><td>{j.name}</td>'
                    f'<td>{j.schedule_type.value}</td>'
                    f'<td>{j.priority.value}</td>'
                    f'<td style="color:{status_color};font-weight:bold">{j.status.value.upper()}</td>'
                    f'<td>{last_run}{last_error}</td>'
                    f'<td>{next_run}</td><td>{j.run_count}</td>'
                    f'<td>{actions}</td></tr>\n'
                )

            # Build run history table (last 20 across all jobs)
            recent_history = all_history[:20]
            history_rows = ""
            for rec in recent_history:
                rec_color = "#4CAF50" if rec.status == "success" else "#f44336"
                err_text = ""
                if rec.error:
                    escaped = rec.error.replace('"', '&quot;').replace('<', '&lt;')[:100]
                    err_text = f'<span title="{escaped}" style="color:#f44336;cursor:help">[details]</span>'
                history_rows += (
                    f'<tr><td><code>{rec.job_id}</code></td>'
                    f'<td>{rec.timestamp[:19]}</td>'
                    f'<td style="color:{rec_color}">{rec.status.upper()}</td>'
                    f'<td>{rec.duration_seconds:.1f}s</td>'
                    f'<td>{rec.attempt_count}</td>'
                    f'<td>{err_text}</td></tr>\n'
                )

            history_table = ""
            if history_rows:
                history_table = (
                    '<table><tr><th>Job ID</th><th>Timestamp</th><th>Status</th>'
                    '<th>Duration</th><th>Attempts</th><th>Error</th></tr>\n'
                    + history_rows + '</table>'
                )
            else:
                history_table = '<p style="color:#9E9E9E">No run history recorded yet.</p>'

            # Stub executor warning
            stub_warning = (
                '<div style="background:#3e2723;border:2px solid #ff9800;border-radius:8px;'
                'padding:15px;margin-bottom:20px;color:#ffcc02">'
                '<strong>WARNING: Stub Executor Active</strong><br>'
                'All jobs are currently running with the DEFAULT EXECUTOR (stub), '
                'which logs the job but does not perform real work. '
                'To wire real executors, implement a custom executor function and pass it '
                'to <code>JobScheduler(executor=your_fn)</code>. '
                'Executor mapping by agent type (claude-code, gemini, shell) is planned '
                'but not yet implemented.'
                '</div>'
            )

            html = f"""<!DOCTYPE html>
<html><head><title>SWE-Squad Scheduler</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; margin-bottom: 20px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #0f3460; font-size: 13px; }}
th {{ color: #e94560; }}
nav {{ margin-bottom: 20px; }}
nav a {{ color: #e94560; margin-right: 15px; text-decoration: none; }}
button {{ background: #0f3460; color: #e0e0e0; border: 1px solid #e94560; border-radius: 4px;
          padding: 4px 10px; cursor: pointer; margin: 1px; font-family: monospace; font-size: 12px; }}
button:hover {{ background: #e94560; color: #fff; }}
.add-btn {{ background: #4CAF50; border-color: #4CAF50; color: #fff; padding: 8px 16px; font-size: 14px; margin-bottom: 15px; }}
.add-btn:hover {{ background: #388E3C; }}
h2 {{ color: #e94560; margin-top: 30px; }}
code {{ background: #0f3460; padding: 2px 6px; border-radius: 3px; }}
#addJobForm {{ display:none; background:#16213e; padding:20px; border-radius:8px; margin-bottom:20px; border:1px solid #0f3460; }}
#addJobForm label {{ display:block; margin-top:10px; color:#e94560; font-size:13px; }}
#addJobForm input, #addJobForm select, #addJobForm textarea {{
    width: 100%; padding: 8px; margin-top: 4px; background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; font-family: monospace; box-sizing: border-box;
}}
#addJobForm textarea {{ height: 60px; resize: vertical; }}
.form-row {{ display: flex; gap: 15px; }}
.form-row > div {{ flex: 1; }}
</style>
<script>
function jobAction(jobId, action) {{
  if (action === 'delete' && !confirm('Permanently delete job ' + jobId + '?')) return;
  fetch('/api/jobs/' + jobId + '/' + action, {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{ if (d.error) alert(d.error); else location.reload(); }})
    .catch(e => alert('Request failed: ' + e));
}}
function toggleAddForm() {{
  var f = document.getElementById('addJobForm');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
}}
function submitNewJob() {{
  var form = document.getElementById('addJobForm');
  var data = {{
    name: form.querySelector('[name=name]').value,
    description: form.querySelector('[name=description]').value,
    schedule_type: form.querySelector('[name=schedule_type]').value,
    cron_expression: form.querySelector('[name=cron_expression]').value,
    interval_minutes: parseInt(form.querySelector('[name=interval_minutes]').value) || 0,
    priority: form.querySelector('[name=priority]').value,
    instructions: form.querySelector('[name=instructions]').value,
    model: form.querySelector('[name=model]').value,
  }};
  if (!data.name) {{ alert('Job name is required'); return; }}
  fetch('/api/jobs', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(data)
  }})
    .then(r => r.json())
    .then(d => {{ if (d.error) alert(d.error); else location.reload(); }})
    .catch(e => alert('Failed to create job: ' + e));
}}
</script>
</head><body>
<nav><a href="/">Dashboard</a> <a href="/costs">Costs</a> <a href="/scheduler">Scheduler</a> <a href="/data">API</a></nav>
<h1>Job Scheduler</h1>
{stub_warning}
<p>{len(jobs)} jobs configured</p>
<button class="add-btn" onclick="toggleAddForm()">+ Add Job</button>
<div id="addJobForm">
<h3 style="margin-top:0;color:#e94560">Create New Job</h3>
<div class="form-row">
  <div><label>Name<input type="text" name="name" placeholder="my-scheduled-task"></label></div>
  <div><label>Priority<select name="priority"><option value="normal">Normal</option><option value="high">High</option><option value="critical">Critical</option><option value="low">Low</option></select></label></div>
  <div><label>Model<select name="model"><option value="sonnet">Sonnet (T2)</option><option value="haiku">Haiku (T1)</option><option value="opus">Opus (T3)</option></select></label></div>
</div>
<label>Description<input type="text" name="description" placeholder="What this job does"></label>
<div class="form-row">
  <div><label>Schedule Type<select name="schedule_type"><option value="cron">Cron</option><option value="interval">Interval</option><option value="once">Once</option></select></label></div>
  <div><label>Cron Expression<input type="text" name="cron_expression" placeholder="*/30 * * * *"></label></div>
  <div><label>Interval (minutes)<input type="number" name="interval_minutes" value="0" min="0"></label></div>
</div>
<label>Instructions<textarea name="instructions" placeholder="Prompt or command for the executor"></textarea></label>
<br>
<button onclick="submitNewJob()" style="background:#4CAF50;border-color:#4CAF50;color:#fff;padding:8px 20px;margin-top:10px">Create Job</button>
<button onclick="toggleAddForm()" style="margin-top:10px">Cancel</button>
</div>
<table>
<tr><th>ID</th><th>Name</th><th>Type</th><th>Priority</th><th>Status</th><th>Last Run</th><th>Next Run</th><th>Runs</th><th>Actions</th></tr>
{rows}</table>
<h2>Run History (Last 20)</h2>
{history_table}
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

    def _json_response(self, data, status: int = 200):
        try:
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            self.send_response(status)
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
