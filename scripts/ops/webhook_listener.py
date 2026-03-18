#!/usr/bin/env python3
"""Lightweight GitHub webhook listener for instant code propagation.

Listens for push events on a configurable port and triggers
scripts/ops/propagate.sh to sync all registered workers immediately.

Stdlib only — no Flask, no FastAPI, no external deps.

Usage:
    python3 scripts/ops/webhook_listener.py                  # port 9876
    python3 scripts/ops/webhook_listener.py --port 9876
    WEBHOOK_SECRET=mysecret python3 scripts/ops/webhook_listener.py

Environment:
    WEBHOOK_PORT     — listen port (default: 9876)
    WEBHOOK_SECRET   — GitHub webhook secret for HMAC validation (optional but recommended)
    SWE_SSH_CONFIG   — passed through to propagate.sh

Security:
    - Validates X-Hub-Signature-256 if WEBHOOK_SECRET is set
    - Only acts on push events to the main branch
    - Runs propagate.sh as a subprocess (inherits env)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("webhook")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROPAGATE_SCRIPT = PROJECT_ROOT / "scripts" / "ops" / "propagate.sh"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Map repo full_name → project flag for propagate.sh
REPO_PROJECT_MAP = {
    "ArtemisAI/SWE-Squad-DEV": "swe-squad",
    "ArtemisAI/SWE-Squad": "swe-squad",
    "ArtemisAI/LinkedAi": "linkedai",
}


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub HMAC-SHA256 signature."""
    if not WEBHOOK_SECRET:
        return True  # no secret configured, skip validation
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def trigger_propagation(project: str, ref: str, pusher: str) -> None:
    """Run propagate.sh in background thread."""
    def _run():
        logger.info("Propagating %s (ref=%s, pusher=%s)", project, ref, pusher)
        try:
            result = subprocess.run(
                ["bash", str(PROPAGATE_SCRIPT), "--project", project],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(PROJECT_ROOT),
            )
            for line in result.stdout.strip().splitlines():
                logger.info("  %s", line)
            if result.returncode != 0:
                logger.warning("propagate.sh exited %d: %s", result.returncode, result.stderr[:300])
        except subprocess.TimeoutExpired:
            logger.error("propagate.sh timed out after 120s")
        except Exception:
            logger.exception("Failed to run propagate.sh")

    Thread(target=_run, daemon=True).start()


class WebhookHandler(BaseHTTPRequestHandler):
    """Handle GitHub webhook POST requests."""

    def do_POST(self):  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(content_length)

        # Signature check
        signature = self.headers.get("X-Hub-Signature-256", "")
        if WEBHOOK_SECRET and not verify_signature(payload, signature):
            logger.warning("Invalid signature from %s", self.client_address[0])
            self._respond(403, "Invalid signature")
            return

        # Parse event
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            logger.info("Ping received — webhook is active")
            self._respond(200, "pong")
            return

        if event != "push":
            self._respond(200, f"Ignored event: {event}")
            return

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self._respond(400, "Invalid JSON")
            return

        # Only propagate pushes to main branch
        ref = data.get("ref", "")
        if ref not in ("refs/heads/main", "refs/heads/master"):
            logger.info("Ignoring push to %s", ref)
            self._respond(200, f"Ignored ref: {ref}")
            return

        repo_name = data.get("repository", {}).get("full_name", "")
        project = REPO_PROJECT_MAP.get(repo_name)
        if not project:
            logger.info("Ignoring push to unmapped repo: %s", repo_name)
            self._respond(200, f"Unmapped repo: {repo_name}")
            return

        pusher = data.get("pusher", {}).get("name", "unknown")
        commits = len(data.get("commits", []))
        logger.info("Push to %s/%s by %s (%d commits)", repo_name, ref, pusher, commits)

        trigger_propagation(project, ref, pusher)
        self._respond(200, f"Propagating {project}")

    def do_GET(self):  # noqa: N802
        """Health check endpoint."""
        if self.path == "/health":
            self._respond(200, json.dumps({
                "status": "ok",
                "service": "swe-squad-webhook",
                "projects": list(REPO_PROJECT_MAP.values()),
            }))
        else:
            self._respond(404, "Not found")

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):  # noqa: A002
        """Suppress default access logs — we log ourselves."""
        pass


def main():
    parser = argparse.ArgumentParser(description="GitHub webhook listener for code propagation")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "9876")))
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()

    if not PROPAGATE_SCRIPT.is_file():
        logger.error("propagate.sh not found at %s", PROPAGATE_SCRIPT)
        sys.exit(1)

    server = HTTPServer((args.bind, args.port), WebhookHandler)
    logger.info("Webhook listener started on %s:%d", args.bind, args.port)
    logger.info("Mapped repos: %s", REPO_PROJECT_MAP)
    if WEBHOOK_SECRET:
        logger.info("HMAC signature validation: ENABLED")
    else:
        logger.warning("HMAC signature validation: DISABLED (set WEBHOOK_SECRET)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
