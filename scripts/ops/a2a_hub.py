#!/usr/bin/env python3
"""
Standalone A2A Hub — runs the SWE-Squad A2A server as a standalone process.

Usage::

    python scripts/ops/a2a_hub.py --port 18790
    python scripts/ops/a2a_hub.py --host 0.0.0.0 --port 18790 --verbose

The server exposes:
  - GET /.well-known/agent-card.json  — agent card discovery
  - POST /a2a                         — JSON-RPC 2.0 endpoint
  - GET /health                       — health check
"""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.a2a.server import A2AServer
from src.a2a.adapters.swe_team import SWETeamAdapter
from src.swe_team.config import load_config
from src.swe_team.ticket_store import TicketStore

logger = logging.getLogger("a2a_hub")


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-Squad A2A Hub Server")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=18790,
        help="Bind port (default: 18790)",
    )
    parser.add_argument(
        "--config",
        help="Path to swe_team.yaml configuration",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )

    config = load_config(args.config)
    store = TicketStore(config.ticket_store_path)

    adapter = SWETeamAdapter(
        config=config,
        store=store,
        base_url=f"http://{args.host}:{args.port}",
    )

    server = A2AServer(adapter=adapter, host=args.host, port=args.port)

    shutdown = threading.Event()

    def _signal_handler(signum, _frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info("Starting A2A Hub on %s:%d", args.host, args.port)
    server.start()
    logger.info(
        "A2A Hub running — agent card at http://%s:%d/.well-known/agent-card.json",
        args.host,
        args.port,
    )

    try:
        shutdown.wait()
    finally:
        server.stop()
        logger.info("A2A Hub stopped")


if __name__ == "__main__":
    main()
