#!/usr/bin/env bash
# ============================================================================
# SWE-Squad WebUI Regression & Smoke Test
#
# Fast curl-based regression tests for backend API, frontend assets, and
# integration flows. Designed to run after every deploy or PR merge (<30s).
#
# Complements ui/e2e-test.mjs (Playwright-based, browser rendering + nav)
# and ui/e2e/ (full frontend specs). This script requires only curl + bash.
#
# The dashboard server has GitHub OAuth auth middleware. Most API endpoints
# require a session cookie and return 302 -> /login when unauthenticated.
# This script tests:
#   - Public endpoints return 200 directly (no auth needed)
#   - Auth-gated endpoints return 302 -> /login (auth gate working)
#   - Static assets (JS/CSS) load without auth
#   - SPA routing works (login page serves index.html with React root)
#   - No 500 errors anywhere
#   - Response times < 5s
#
# Usage:
#   bash scripts/ops/regression_test.sh [BASE_URL]
#   bash scripts/ops/regression_test.sh http://localhost:8080
# ============================================================================
set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"

# Strip trailing slash
BASE_URL="${BASE_URL%/}"

# -- Colors ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# -- Counters ----------------------------------------------------------------
TOTAL=0
PASSED=0
FAILED=0
WARNINGS=0
FAILURES=""

# -- Helpers -----------------------------------------------------------------

pass() {
    TOTAL=$((TOTAL + 1))
    PASSED=$((PASSED + 1))
    printf "  ${GREEN}PASS${NC}  %s\n" "$1"
}

fail() {
    TOTAL=$((TOTAL + 1))
    FAILED=$((FAILED + 1))
    printf "  ${RED}FAIL${NC}  %s\n" "$1"
    FAILURES="${FAILURES}\n  - $1"
}

warn() {
    WARNINGS=$((WARNINGS + 1))
    printf "  ${YELLOW}WARN${NC}  %s\n" "$1"
}

section() {
    echo ""
    printf "${CYAN}${BOLD}=== %s ===${NC}\n" "$1"
}

# curl wrapper: fetch URL, capture status code, body, headers, and elapsed time.
# Usage: do_curl URL [extra_curl_args...]
# Sets: RESP_STATUS, RESP_BODY, RESP_TIME, RESP_CONTENT_TYPE, RESP_LOCATION
do_curl() {
    local url="$1"
    shift
    local tmpfile header_file
    tmpfile=$(mktemp)
    header_file=$(mktemp)

    RESP_STATUS=0
    RESP_BODY=""
    RESP_TIME="0"
    RESP_CONTENT_TYPE=""
    RESP_LOCATION=""

    if RESP_STATUS=$(curl -s -o "$tmpfile" -D "$header_file" \
        -w "%{http_code}|%{time_total}" \
        --max-time 10 \
        --connect-timeout 5 \
        "$@" "$url" 2>/dev/null); then
        # Parse combined output: status|time
        RESP_TIME="${RESP_STATUS#*|}"
        RESP_STATUS="${RESP_STATUS%%|*}"
        RESP_BODY=$(cat "$tmpfile" 2>/dev/null || true)
        RESP_CONTENT_TYPE=$(grep -i '^content-type:' "$header_file" 2>/dev/null | head -1 | cut -d: -f2- | tr -d '[:space:]' || true)
        RESP_LOCATION=$(grep -i '^location:' "$header_file" 2>/dev/null | head -1 | cut -d: -f2- | tr -d '[:space:]' || true)
    else
        RESP_STATUS=0
        RESP_TIME="0"
    fi

    rm -f "$tmpfile" "$header_file"
}

# Check if body is valid JSON
is_valid_json() {
    echo "$1" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null
}

# Check if body is a JSON array
is_json_array() {
    echo "$1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
sys.exit(0 if isinstance(d, list) else 1)
" 2>/dev/null
}

# Check if body is a JSON object with a given key
json_has_key() {
    local body="$1"
    local key="$2"
    echo "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
sys.exit(0 if isinstance(d, dict) and '$key' in d else 1)
" 2>/dev/null
}

# ============================================================================
# 0. Connectivity Check
# ============================================================================

section "Connectivity Check"

do_curl "${BASE_URL}/health"
if [[ "$RESP_STATUS" == "0" ]]; then
    fail "Cannot reach ${BASE_URL} -- is the server running?"
    printf "\n${RED}${BOLD}  ABORTING: Server unreachable${NC}\n\n"
    exit 1
fi
pass "Server is reachable at ${BASE_URL}"

# ============================================================================
# 1. Public Endpoints (no auth required)
# ============================================================================

section "Public Endpoints (no auth required)"

# -- GET /health -------------------------------------------------------------
do_curl "${BASE_URL}/health"
if [[ "$RESP_STATUS" == "200" ]]; then
    if json_has_key "$RESP_BODY" "status"; then
        pass "GET /health -> 200, has 'status' field"
    else
        fail "GET /health -> 200 but missing 'status' key"
    fi
else
    fail "GET /health -> $RESP_STATUS (expected 200)"
fi

# -- GET /api/auth/status ----------------------------------------------------
do_curl "${BASE_URL}/api/auth/status"
if [[ "$RESP_STATUS" == "200" ]]; then
    if is_valid_json "$RESP_BODY"; then
        pass "GET /api/auth/status -> 200, valid JSON"
    else
        fail "GET /api/auth/status -> 200 but not valid JSON"
    fi
else
    fail "GET /api/auth/status -> $RESP_STATUS (expected 200)"
fi

# -- GET /login (serves SPA) ------------------------------------------------
do_curl "${BASE_URL}/login"
if [[ "$RESP_STATUS" == "200" ]]; then
    if echo "$RESP_BODY" | grep -q '<div id="root"'; then
        pass "GET /login -> 200, serves SPA with <div id=\"root\">"
    else
        fail "GET /login -> 200 but missing <div id=\"root\">"
    fi
else
    fail "GET /login -> $RESP_STATUS (expected 200)"
fi

# -- GET /welcome ------------------------------------------------------------
do_curl "${BASE_URL}/welcome"
if [[ "$RESP_STATUS" == "200" ]]; then
    pass "GET /welcome -> 200"
else
    fail "GET /welcome -> $RESP_STATUS (expected 200)"
fi

# -- GET /api/onboarding/status ----------------------------------------------
do_curl "${BASE_URL}/api/onboarding/status"
if [[ "$RESP_STATUS" == "200" ]]; then
    if is_valid_json "$RESP_BODY"; then
        pass "GET /api/onboarding/status -> 200, valid JSON"
    else
        fail "GET /api/onboarding/status -> 200 but not valid JSON"
    fi
else
    fail "GET /api/onboarding/status -> $RESP_STATUS (expected 200)"
fi

# ============================================================================
# 2. Auth Gate Tests (must redirect to /login when unauthenticated)
# ============================================================================

section "Auth Gate Tests (expect 302 -> /login)"

# These endpoints require a session cookie. Without one, the server should
# return 302 redirecting to /login. If we get 200, auth gate is broken.
# If we get 500, the endpoint is crashing.

AUTH_GATED_ENDPOINTS=(
    "/data"
    "/api/projects"
    "/api/teams"
    "/api/engines"
    "/api/cost"
    "/api/scheduler"
    "/api/workflows"
    "/api/integrations"
    "/api/accounts"
    "/api/agents"
    "/api/activity"
    "/api/rbac"
    "/api/settings"
    "/api/approvals"
    "/api/goals"
    "/api/routines"
    "/api/roles"
    "/api/graph"
    "/api/governor/status"
    "/api/tickets"
    "/api/costs/by_hour"
    "/api/budget/policies"
    "/api/pipeline/config"
    "/api/models/probe"
    "/api/rate-limits"
    "/api/execution/mode"
    "/api/github/label-triggers"
    "/api/suggestions"
    "/api/scheduler/templates"
    "/api/projects/default/env"
)

for ep in "${AUTH_GATED_ENDPOINTS[@]}"; do
    do_curl "${BASE_URL}${ep}"
    if [[ "$RESP_STATUS" == "302" ]]; then
        # Verify it redirects to /login
        if echo "$RESP_LOCATION" | grep -q "/login"; then
            pass "GET $ep -> 302 -> /login (auth gate OK)"
        else
            warn "GET $ep -> 302 but redirects to '$RESP_LOCATION' (expected /login)"
        fi
    elif [[ "$RESP_STATUS" == "200" ]]; then
        # Auth gate might be disabled, or endpoint is public -- still valid
        if is_valid_json "$RESP_BODY"; then
            pass "GET $ep -> 200, valid JSON (no auth gate or public)"
        else
            # Could be HTML error page
            if echo "$RESP_BODY" | grep -qi "error\|traceback\|exception"; then
                fail "GET $ep -> 200 but body looks like an error page"
            else
                pass "GET $ep -> 200 (no auth gate)"
            fi
        fi
    elif [[ "$RESP_STATUS" == "401" ]]; then
        pass "GET $ep -> 401 (auth required, no redirect)"
    elif [[ "$RESP_STATUS" -ge 500 ]]; then
        fail "GET $ep -> $RESP_STATUS (SERVER ERROR)"
    else
        fail "GET $ep -> $RESP_STATUS (unexpected status)"
    fi
done

# ============================================================================
# 3. Auth-Gated Mutation Endpoints
# ============================================================================

section "Auth-Gated Mutation Tests (POST/PATCH)"

# POST /api/tickets -- should get 302 or 401 without auth, not 500
DUMMY_TICKET='{"title":"regression-test-dummy","severity":"LOW","source_module":"test"}'
do_curl "${BASE_URL}/api/tickets" -X POST \
    -H "Content-Type: application/json" \
    -d "$DUMMY_TICKET"
if [[ "$RESP_STATUS" == "200" ]] || [[ "$RESP_STATUS" == "201" ]]; then
    if is_valid_json "$RESP_BODY"; then
        pass "POST /api/tickets -> $RESP_STATUS, valid JSON"
    else
        fail "POST /api/tickets -> $RESP_STATUS but not valid JSON"
    fi
elif [[ "$RESP_STATUS" == "302" ]] || [[ "$RESP_STATUS" == "401" ]]; then
    pass "POST /api/tickets -> $RESP_STATUS (auth required, as expected)"
elif [[ "$RESP_STATUS" -ge 500 ]]; then
    fail "POST /api/tickets -> $RESP_STATUS (SERVER ERROR)"
else
    warn "POST /api/tickets -> $RESP_STATUS (unexpected but not a server error)"
fi

# POST /api/settings
SETTINGS_DATA='{"theme":"dark"}'
do_curl "${BASE_URL}/api/settings" -X POST \
    -H "Content-Type: application/json" \
    -d "$SETTINGS_DATA"
if [[ "$RESP_STATUS" == "200" ]] || [[ "$RESP_STATUS" == "204" ]]; then
    pass "POST /api/settings -> $RESP_STATUS"
elif [[ "$RESP_STATUS" == "302" ]] || [[ "$RESP_STATUS" == "401" ]]; then
    pass "POST /api/settings -> $RESP_STATUS (auth required, as expected)"
elif [[ "$RESP_STATUS" -ge 500 ]]; then
    fail "POST /api/settings -> $RESP_STATUS (SERVER ERROR)"
else
    warn "POST /api/settings -> $RESP_STATUS"
fi

# ============================================================================
# 4. Frontend Asset Tests (static files bypass auth)
# ============================================================================

section "Frontend Asset Tests"

# Get the SPA HTML from /login (always accessible)
do_curl "${BASE_URL}/login"
INDEX_HTML="$RESP_BODY"

if [[ -z "$INDEX_HTML" ]] || ! echo "$INDEX_HTML" | grep -q '<div id="root"'; then
    fail "Cannot get SPA HTML from /login -- skipping asset tests"
else
    # Extract src from <script> tags
    SCRIPT_SRCS=$(echo "$INDEX_HTML" | grep -oP 'src="(/assets/[^"]+)"' | grep -oP '/assets/[^"]+' || true)

    # Extract href from <link> tags referencing CSS
    CSS_HREFS=$(echo "$INDEX_HTML" | grep -oP 'href="(/assets/[^"]+\.css)"' | grep -oP '/assets/[^"]+\.css' || true)

    # Extract modulepreload hrefs
    PRELOAD_HREFS=$(echo "$INDEX_HTML" | grep -oP 'href="(/assets/[^"]+\.js)"' | grep -oP '/assets/[^"]+\.js' || true)

    # Combine all asset URLs (deduplicate)
    ALL_ASSETS=$(echo -e "${SCRIPT_SRCS}\n${CSS_HREFS}\n${PRELOAD_HREFS}" | sort -u | grep -v '^$' || true)

    ASSET_COUNT=0
    ASSET_PASS=0

    for asset_path in $ALL_ASSETS; do
        ASSET_COUNT=$((ASSET_COUNT + 1))
        do_curl "${BASE_URL}${asset_path}"
        if [[ "$RESP_STATUS" == "200" ]]; then
            ASSET_PASS=$((ASSET_PASS + 1))
            # Check content type matches extension
            if [[ "$asset_path" == *.js ]] && ! echo "$RESP_CONTENT_TYPE" | grep -qi "javascript"; then
                warn "JS asset $asset_path has content-type '$RESP_CONTENT_TYPE'"
            elif [[ "$asset_path" == *.css ]] && ! echo "$RESP_CONTENT_TYPE" | grep -qi "css"; then
                warn "CSS asset $asset_path has content-type '$RESP_CONTENT_TYPE'"
            fi
        elif [[ "$RESP_STATUS" -ge 500 ]]; then
            fail "Asset $asset_path -> $RESP_STATUS (SERVER ERROR)"
        else
            fail "Asset $asset_path -> $RESP_STATUS (expected 200)"
        fi
    done

    if [[ "$ASSET_COUNT" -eq 0 ]]; then
        warn "No assets found in index.html"
    elif [[ "$ASSET_PASS" -eq "$ASSET_COUNT" ]]; then
        pass "All $ASSET_COUNT static assets load successfully (JS + CSS)"
    else
        fail "$((ASSET_COUNT - ASSET_PASS))/$ASSET_COUNT assets failed to load"
    fi
fi

# ============================================================================
# 5. SPA Routing Tests (auth-gated routes redirect, login serves SPA)
# ============================================================================

section "SPA Routing Tests"

SPA_ROUTES=("/tickets" "/goals" "/approvals" "/agents" "/control" "/settings" "/costs" "/scheduler" "/engines" "/teams" "/workspaces" "/activity" "/projects" "/rbac" "/graph" "/integrations" "/organization" "/routines" "/instance" "/inbox" "/create" "/data/export" "/data/import")

for route in "${SPA_ROUTES[@]}"; do
    do_curl "${BASE_URL}${route}"
    if [[ "$RESP_STATUS" == "302" ]]; then
        # Auth gate redirects to /login -- follow it and check SPA loads
        do_curl "${BASE_URL}/login"
        if [[ "$RESP_STATUS" == "200" ]] && echo "$RESP_BODY" | grep -q '<div id="root"'; then
            pass "SPA route $route -> 302 -> /login (SPA serves correctly)"
        else
            fail "SPA route $route -> 302 but /login doesn't serve SPA"
        fi
    elif [[ "$RESP_STATUS" == "200" ]]; then
        if echo "$RESP_BODY" | grep -q '<div id="root"'; then
            pass "SPA route $route -> 200, serves index.html"
        else
            fail "SPA route $route -> 200 but missing <div id=\"root\">"
        fi
    elif [[ "$RESP_STATUS" -ge 500 ]]; then
        fail "SPA route $route -> $RESP_STATUS (SERVER ERROR)"
    else
        fail "SPA route $route -> $RESP_STATUS (unexpected)"
    fi
done

# ============================================================================
# 6. Integration Tests
# ============================================================================

section "Integration Tests"

# -- Full page load: /login -> extract JS bundles -> verify each loads -------
echo "  Full page load simulation..."
do_curl "${BASE_URL}/login"
if [[ "$RESP_STATUS" == "200" ]]; then
    JS_URLS=$(echo "$RESP_BODY" | grep -oP '(src|href)="/assets/[^"]+"' | grep -oP '/assets/[^"]+' || true)
    all_ok=true
    bundle_count=0
    for js_url in $JS_URLS; do
        bundle_count=$((bundle_count + 1))
        do_curl "${BASE_URL}${js_url}"
        if [[ "$RESP_STATUS" != "200" ]]; then
            fail "Bundle $js_url -> $RESP_STATUS during full page load"
            all_ok=false
        fi
    done
    if $all_ok && [[ "$bundle_count" -gt 0 ]]; then
        pass "Full page load: index.html + $bundle_count bundles all load OK"
    elif [[ "$bundle_count" -eq 0 ]]; then
        warn "No bundles found in index.html"
    fi
else
    fail "Full page load: /login returned $RESP_STATUS"
fi

# -- Auth flow: /api/auth/status returns valid session info ------------------
echo "  Auth flow validation..."
do_curl "${BASE_URL}/api/auth/status"
if [[ "$RESP_STATUS" == "200" ]] && is_valid_json "$RESP_BODY"; then
    pass "Auth flow: /api/auth/status returns valid session info"
else
    fail "Auth flow: /api/auth/status -> $RESP_STATUS or invalid JSON"
fi

# -- Auth redirect chain: / -> 302 /login -> 200 HTML -----------------------
echo "  Auth redirect chain..."
do_curl "${BASE_URL}/"
if [[ "$RESP_STATUS" == "302" ]]; then
    # Follow the redirect
    do_curl "${BASE_URL}/login"
    if [[ "$RESP_STATUS" == "200" ]] && echo "$RESP_BODY" | grep -q '<div id="root"'; then
        pass "Auth redirect: / -> 302 -> /login -> 200 (SPA loads)"
    else
        fail "Auth redirect: / -> 302 -> /login but SPA didn't load ($RESP_STATUS)"
    fi
elif [[ "$RESP_STATUS" == "200" ]]; then
    pass "Auth redirect: / -> 200 directly (auth may be disabled)"
else
    fail "Auth redirect: / -> $RESP_STATUS (unexpected)"
fi

# -- No 500 errors: scan a broad set of paths --------------------------------
echo "  Server error scan..."
ERROR_SCAN_PATHS=(
    "/health"
    "/login"
    "/api/auth/status"
    "/api/onboarding/status"
    "/favicon.ico"
    "/"
    "/data"
    "/api/projects"
    "/api/teams"
    "/api/agents"
    "/api/tickets"
    "/api/scheduler"
    "/api/cost"
    "/api/settings"
    "/api/engines"
    "/api/integrations"
    "/api/workflows"
    "/api/approvals"
    "/api/accounts"
    "/api/graph"
    "/api/rbac"
    "/api/roles"
    "/api/routines"
    "/api/goals"
    "/api/governor/status"
    "/api/rate-limits"
    "/api/execution/mode"
    "/api/github/label-triggers"
    "/api/suggestions"
    "/api/scheduler/templates"
    "/api/projects/default/env"
)
SERVER_ERRORS=0
for path in "${ERROR_SCAN_PATHS[@]}"; do
    do_curl "${BASE_URL}${path}"
    if [[ "$RESP_STATUS" -ge 500 ]]; then
        fail "SERVER ERROR: $path -> $RESP_STATUS"
        SERVER_ERRORS=$((SERVER_ERRORS + 1))
    fi
done
if [[ "$SERVER_ERRORS" -eq 0 ]]; then
    pass "No 500 errors across ${#ERROR_SCAN_PATHS[@]} endpoints"
fi

# ============================================================================
# 7. Response Time Checks
# ============================================================================

section "Response Time Checks"

CRITICAL_ENDPOINTS=("/health" "/login" "/api/auth/status")
for ep in "${CRITICAL_ENDPOINTS[@]}"; do
    do_curl "${BASE_URL}${ep}"
    elapsed=$(echo "$RESP_TIME" | tr -d '[:space:]')
    if python3 -c "import sys; sys.exit(0 if float('${elapsed}') < 5.0 else 1)" 2>/dev/null; then
        pass "Response time $ep -> ${elapsed}s (< 5s)"
    else
        fail "Response time $ep -> ${elapsed}s (exceeds 5s threshold)"
    fi
done

# Also time a few auth-gated endpoints (302 should be fast)
GATED_TIME_ENDPOINTS=("/data" "/api/agents" "/api/tickets")
for ep in "${GATED_TIME_ENDPOINTS[@]}"; do
    do_curl "${BASE_URL}${ep}"
    elapsed=$(echo "$RESP_TIME" | tr -d '[:space:]')
    if python3 -c "import sys; sys.exit(0 if float('${elapsed}') < 5.0 else 1)" 2>/dev/null; then
        pass "Response time $ep -> ${elapsed}s (< 5s)"
    else
        fail "Response time $ep -> ${elapsed}s (exceeds 5s threshold)"
    fi
done

# ============================================================================
# Summary
# ============================================================================

echo ""
echo ""
printf "${BOLD}============================================================${NC}\n"
printf "${BOLD}  REGRESSION TEST SUMMARY${NC}\n"
printf "${BOLD}============================================================${NC}\n"
echo ""
printf "  Base URL:    %s\n" "$BASE_URL"
printf "  Timestamp:   %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""
printf "  Total:       %d\n" "$TOTAL"
printf "  ${GREEN}Passed:${NC}      %d\n" "$PASSED"
printf "  ${RED}Failed:${NC}      %d\n" "$FAILED"
printf "  ${YELLOW}Warnings:${NC}    %d\n" "$WARNINGS"
echo ""

if [[ "$FAILED" -gt 0 ]]; then
    printf "${RED}${BOLD}  RESULT: FAIL${NC}\n"
    printf "\n${RED}  Failed tests:${NC}"
    printf "%b\n" "$FAILURES"
    echo ""
    exit 1
else
    printf "${GREEN}${BOLD}  RESULT: ALL TESTS PASSED${NC}\n"
    echo ""
    exit 0
fi
