#!/usr/bin/env bash
# =============================================================================
# Gemini CLI MCP Setup — run once per machine / after Gemini reinstall
#
# Configures MCP servers in ~/.gemini/settings.json (user scope, persistent).
# Gemini reads its own settings separately from Claude's ~/.claude.json.
#
# What gets configured:
#   playwright  — browser automation for WebUI/dashboard tasks & UI testing
#   deepwiki    — public GitHub repo docs (HTTP, no auth needed)
#
# What is intentionally NOT added:
#   github      — Gemini is read-only; no write access to repos
#   supabase    — Gemini doesn't query the ticket store directly
#
# Usage:
#   bash scripts/ops/gemini_setup_mcp.sh
#   bash scripts/ops/gemini_setup_mcp.sh --verify   # just list + smoke test
# =============================================================================

set -euo pipefail

VERIFY_ONLY=false
if [[ "${1:-}" == "--verify" ]]; then
  VERIFY_ONLY=true
fi

echo "=== Gemini CLI MCP Setup ==="
echo ""

# --- Verify gemini is installed ---
if ! command -v gemini &>/dev/null; then
  echo "ERROR: gemini CLI not found. Install from https://github.com/google-gemini/gemini-cli"
  exit 1
fi

GEMINI_VERSION=$(gemini --version 2>/dev/null || echo "unknown")
echo "Gemini version: $GEMINI_VERSION"
echo ""

if [[ "$VERIFY_ONLY" == "true" ]]; then
  echo "=== Current MCP servers ==="
  gemini mcp list 2>&1
  echo ""
  echo "=== Smoke test: Playwright (navigate example.com) ==="
  timeout 30 gemini -p \
    "Using playwright, navigate to https://example.com and return just the page title." \
    2>&1 | grep -v "^Loaded\|^Server\|^Listening" || true
  echo ""
  echo "=== Smoke test: DeepWiki (look up a public repo) ==="
  timeout 30 gemini -p \
    "Using deepwiki, what is the one-sentence description of https://github.com/anthropics/claude-code ?" \
    2>&1 | grep -v "^Loaded\|^Server\|^Listening" | tail -5 || true
  exit 0
fi

echo "--- Configuring MCP servers (user scope, persistent) ---"
echo ""

# Remove any stale project-scope entries first
gemini mcp remove playwright 2>/dev/null && echo "Removed stale playwright (project scope)" || true
gemini mcp remove deepwiki   2>/dev/null && echo "Removed stale deepwiki (project scope)"   || true

# Playwright — stdio, trusted (no confirmation prompts for tool calls)
echo "Adding playwright..."
gemini mcp add playwright npx @playwright/mcp@latest \
  --scope user \
  --trust \
  2>&1 | grep -v "^Loaded"
echo ""

# DeepWiki — HTTP transport
echo "Adding deepwiki..."
gemini mcp add deepwiki https://mcp.deepwiki.com/mcp \
  --scope user \
  --transport http \
  2>&1 | grep -v "^Loaded"
echo ""

echo "--- Verifying ---"
gemini mcp list 2>&1 | grep -v "^Loaded"
echo ""

# Quick connectivity smoke test
echo "--- Smoke test: Playwright ---"
RESULT=$(timeout 30 gemini -p \
  "Using playwright, navigate to https://example.com and return just the page title." \
  2>&1 | grep -v "^Loaded\|^Server\|^Listening" | tail -3 || echo "TIMEOUT/FAIL")
echo "$RESULT"

if echo "$RESULT" | grep -qi "example domain\|Example Domain"; then
  echo "✓ Playwright: PASS"
else
  echo "✗ Playwright: FAIL (may need: npx playwright install chromium)"
fi
echo ""

echo "=== Setup complete ==="
echo ""
echo "Gemini CLI can now:"
echo "  - Browse and screenshot any URL (Playwright)"
echo "  - Look up public GitHub repo docs (DeepWiki)"
echo "  - Web search (built-in — no MCP needed)"
echo ""
echo "NOT configured (intentional — Gemini is read-only):"
echo "  - GitHub MCP (no repo write access for Gemini)"
echo "  - Supabase MCP (ticket store queries via Claude only)"
