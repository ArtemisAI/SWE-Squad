#!/usr/bin/env python3
"""
CLI tool for sending requests to A2A agents.

Usage::

    # Discover agents
    python scripts/ops/a2a_request.py discover http://100.96.188.64:18790

    # Send a task
    python scripts/ops/a2a_request.py send http://100.96.188.64:18790 investigate_ticket '{"ticket_id": "gh-17"}'

    # Check task status
    python scripts/ops/a2a_request.py status http://100.96.188.64:18790 task-uuid-here

    # Cancel a task
    python scripts/ops/a2a_request.py cancel http://100.96.188.64:18790 task-uuid-here

    # Health check
    python scripts/ops/a2a_request.py health http://100.96.188.64:18790
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.a2a.client import A2AClient, A2AClientError


def cmd_discover(client: A2AClient, args: argparse.Namespace) -> int:
    """Discover an agent at the given URL."""
    try:
        card = client.discover(args.url)
        print(json.dumps(card, indent=2))
        return 0
    except A2AClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_send(client: A2AClient, args: argparse.Namespace) -> int:
    """Send a task to an agent."""
    payload = {}
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON payload: {exc}", file=sys.stderr)
            return 1

    try:
        result = client.send_task(
            args.url,
            skill_id=args.skill,
            payload=payload,
            timeout=args.timeout,
        )
        print(json.dumps(result, indent=2))
        return 0
    except A2AClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_status(client: A2AClient, args: argparse.Namespace) -> int:
    """Check the status of a task."""
    try:
        result = client.get_task(args.url, task_id=args.task_id)
        print(json.dumps(result, indent=2))
        return 0
    except A2AClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_cancel(client: A2AClient, args: argparse.Namespace) -> int:
    """Cancel a task."""
    try:
        result = client.cancel_task(args.url, task_id=args.task_id)
        print(json.dumps(result, indent=2))
        return 0
    except A2AClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_health(client: A2AClient, args: argparse.Namespace) -> int:
    """Check if an agent is healthy."""
    healthy = client.health_check(args.url)
    if healthy:
        print(f"Agent at {args.url} is healthy")
        return 0
    else:
        print(f"Agent at {args.url} is NOT reachable", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CLI tool for A2A agent communication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default: 30)",
    )

    sub = parser.add_subparsers(dest="command", help="A2A command")

    # discover
    p_discover = sub.add_parser("discover", help="Discover an agent")
    p_discover.add_argument("url", help="Base URL of the agent")

    # send
    p_send = sub.add_parser("send", help="Send a task to an agent")
    p_send.add_argument("url", help="Base URL of the agent")
    p_send.add_argument("skill", help="Skill ID to invoke")
    p_send.add_argument("payload", nargs="?", default="{}", help="JSON payload")

    # status
    p_status = sub.add_parser("status", help="Check task status")
    p_status.add_argument("url", help="Base URL of the agent")
    p_status.add_argument("task_id", help="Task ID to query")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a task")
    p_cancel.add_argument("url", help="Base URL of the agent")
    p_cancel.add_argument("task_id", help="Task ID to cancel")

    # health
    p_health = sub.add_parser("health", help="Health check")
    p_health.add_argument("url", help="Base URL of the agent")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    client = A2AClient(timeout=args.timeout)

    commands = {
        "discover": cmd_discover,
        "send": cmd_send,
        "status": cmd_status,
        "cancel": cmd_cancel,
        "health": cmd_health,
    }

    return commands[args.command](client, args)


if __name__ == "__main__":
    sys.exit(main())
