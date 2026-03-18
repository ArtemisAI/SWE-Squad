#!/usr/bin/env python3
"""PR Sync — synchronise GitHub PRs into the knowledge graph.

Runs every 5 minutes via cron. For each configured repo:
1. Lists recent PRs via `gh pr list`
2. Extracts files changed, ticket references, and review status
3. Upserts PR nodes and creates knowledge edges:
   - resolves: PR → ticket (from branch name or body)
   - touches_module: PR → code module (from files changed)
   - conflicts_with: PR ↔ PR (overlapping files)
   - caused_regression: PR → ticket (new ticket <30min after merge, same module)

Usage:
    python scripts/ops/pr_sync.py [--repos owner/repo ...] [-v]
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project bootstrap ─────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=True)

from src.swe_team.knowledge_store import KnowledgeGraphStore
from src.swe_team.models import (
    CodeModule,
    EdgeType,
    KnowledgeEdge,
    PRNode,
)

logger = logging.getLogger("pr_sync")


def _run_gh(args: List[str], timeout: int = 30) -> Optional[str]:
    """Run a gh CLI command and return stdout, or None on failure.

    Strips GH_TOKEN from the subprocess env so ``gh`` falls through to its
    keyring / ``gh auth`` credential store. This avoids stale PATs loaded
    by dotenv from poisoning API calls.
    """
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning("gh %s failed (rc=%d): %s", " ".join(args[:3]), result.returncode, result.stderr[:200])
        return None
    except Exception:
        logger.warning("gh command failed", exc_info=True)
        return None


def list_prs(repo: str, state: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
    """List PRs from a repo via gh CLI, return as list of dicts."""
    raw = _run_gh([
        "pr", "list", "--repo", repo,
        "--state", state, "--limit", str(limit),
        "--json", "number,title,headRefName,state,author,createdAt,mergedAt,reviewDecision,url",
    ])
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def get_pr_files(repo: str, pr_number: int) -> List[str]:
    """Get files changed in a PR."""
    raw = _run_gh([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "files",
    ])
    if not raw:
        return []
    try:
        data = json.loads(raw)
        files = data.get("files", [])
        return [f.get("path", "") for f in files if f.get("path")]
    except (json.JSONDecodeError, KeyError):
        return []


def extract_ticket_refs(branch: str, title: str = "", body: str = "") -> List[str]:
    """Extract ticket IDs from branch name, title, and body.

    Patterns:
    - Branch: swe-fix/ticket-{id}, fix/{id}, ticket-{id}
    - Text: fixes #id, closes #id, resolves #id, ticket-{hex12}
    """
    refs: set[str] = set()

    # Branch patterns
    branch_patterns = [
        r'swe-fix/ticket-([a-f0-9]{8,12})',
        r'fix/([a-f0-9]{8,12})',
        r'ticket-([a-f0-9]{8,12})',
    ]
    for pat in branch_patterns:
        m = re.search(pat, branch, re.IGNORECASE)
        if m:
            refs.add(m.group(1))

    # Text patterns (title + body)
    for text in [title, body]:
        if not text:
            continue
        # "fixes ticket-abc123" or "closes ticket-abc123"
        for m in re.finditer(r'(?:fixes|closes|resolves)\s+(?:#|ticket-)([a-f0-9]{8,12})', text, re.IGNORECASE):
            refs.add(m.group(1))

    return list(refs)


def module_id_from_path(file_path: str) -> str:
    """Extract a module ID from a file path (e.g. 'src/security.py' -> 'security.py')."""
    return Path(file_path).name


def sync_repo(repo: str, store: KnowledgeGraphStore, team_id: str) -> Dict[str, int]:
    """Sync PRs from a single repo. Returns counts of operations."""
    stats = {"prs_synced": 0, "edges_created": 0, "modules_created": 0}

    prs = list_prs(repo, state="all", limit=50)
    if not prs:
        logger.info("No PRs found for %s", repo)
        return stats

    # Collect all open PRs for conflict detection
    open_prs: List[PRNode] = []

    for pr_data in prs:
        number = pr_data.get("number", 0)
        if not number:
            continue

        pr_id = f"{repo}#{number}"
        branch = pr_data.get("headRefName", "")
        title = pr_data.get("title", "")
        author = pr_data.get("author", {})
        author_login = author.get("login", "") if isinstance(author, dict) else str(author)

        # Map GH state to our status
        gh_state = (pr_data.get("state") or "").upper()
        if gh_state == "MERGED":
            status = "merged"
        elif gh_state == "CLOSED":
            status = "closed"
        else:
            status = "open"

        # Map review decision
        review_decision = (pr_data.get("reviewDecision") or "").upper()
        review_map = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
        }
        review_status = review_map.get(review_decision, "pending")

        # Get files changed
        files = get_pr_files(repo, number)

        # Extract ticket references
        ticket_ids = extract_ticket_refs(branch, title)

        # Parse timestamps
        created_at = pr_data.get("createdAt", "")
        merged_at = pr_data.get("mergedAt")

        pr_node = PRNode(
            pr_id=pr_id,
            repo=repo,
            number=number,
            branch=branch,
            title=title,
            status=status,
            author=author_login,
            files_changed=files,
            ticket_ids=ticket_ids,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            merged_at=merged_at,
            review_status=review_status,
        )

        try:
            store.upsert_pr_node(pr_node)
            stats["prs_synced"] += 1
        except Exception:
            logger.warning("Failed to upsert PR %s", pr_id, exc_info=True)
            continue

        if status == "open":
            open_prs.append(pr_node)

        # Create edges
        edges: List[KnowledgeEdge] = []

        # resolves edges (PR -> ticket)
        for tid in ticket_ids:
            edges.append(KnowledgeEdge(
                source_id=pr_id,
                target_id=tid,
                edge_type=EdgeType.RESOLVES,
                confidence=0.9,
                discovered_by="pr_sync",
            ))

        # touches_module edges (PR -> module)
        for f in files:
            mid = module_id_from_path(f)
            if mid:
                try:
                    store.upsert_module(CodeModule(
                        module_id=mid,
                        repo=repo,
                        file_path=f,
                    ))
                    stats["modules_created"] += 1
                except Exception:
                    pass

                edges.append(KnowledgeEdge(
                    source_id=pr_id,
                    target_id=mid,
                    edge_type=EdgeType.TOUCHES_MODULE,
                    confidence=1.0,
                    discovered_by="pr_sync",
                ))

        if edges:
            try:
                count = store.create_edges_batch(edges)
                stats["edges_created"] += count
            except Exception:
                logger.warning("Failed to create edges for PR %s", pr_id, exc_info=True)

    # Detect conflicts between open PRs
    for i, pr_a in enumerate(open_prs):
        files_a = set(pr_a.files_changed)
        if not files_a:
            continue
        for pr_b in open_prs[i + 1:]:
            files_b = set(pr_b.files_changed)
            overlap = files_a & files_b
            if overlap:
                confidence = len(overlap) / max(len(files_a), 1)
                edge = KnowledgeEdge(
                    source_id=pr_a.pr_id,
                    target_id=pr_b.pr_id,
                    edge_type=EdgeType.CONFLICTS_WITH,
                    confidence=min(confidence, 1.0),
                    discovered_by="pr_sync",
                    metadata={"overlapping_files": list(overlap)[:10]},
                )
                try:
                    store.create_edge(edge)
                    stats["edges_created"] += 1
                except Exception:
                    pass

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub PRs into knowledge graph")
    parser.add_argument("--repos", nargs="+", help="Repos to sync (default: from SWE_GITHUB_REPO)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    repos = args.repos or []
    if not repos:
        default_repo = os.environ.get("SWE_GITHUB_REPO", "")
        if default_repo:
            repos = [default_repo]

    if not repos:
        logger.error("No repos configured. Set SWE_GITHUB_REPO or pass --repos")
        sys.exit(1)

    team_id = os.environ.get("SWE_TEAM_ID", "default")
    store = KnowledgeGraphStore(team_id=team_id)

    total_stats = {"prs_synced": 0, "edges_created": 0, "modules_created": 0}
    for repo in repos:
        logger.info("Syncing PRs from %s", repo)
        stats = sync_repo(repo, store, team_id)
        for k, v in stats.items():
            total_stats[k] += v

    logger.info(
        "PR sync complete: %d PRs, %d edges, %d modules",
        total_stats["prs_synced"],
        total_stats["edges_created"],
        total_stats["modules_created"],
    )


if __name__ == "__main__":
    main()
