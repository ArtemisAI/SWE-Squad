# Testing Infrastructure

## Unit Tests

| Property | Value |
|----------|-------|
| Location | `tests/unit/` |
| Count | 5,931+ tests |
| Dependencies | None (no network, no API keys, no external services) |
| Framework | pytest |

### Running

```bash
# Full suite (verbose)
python3 -m pytest tests/unit/ -v --tb=short

# Quick run (quiet output)
python3 -m pytest tests/unit/ -q

# Via Makefile
make test

# Single file
python3 -m pytest tests/unit/test_monitor.py -v

# Single test
python3 -m pytest tests/unit/test_monitor.py::test_scan_deduplicates -v
```

All unit tests must pass before any commit to main.

## Regression Test Suite

| Property | Value |
|----------|-------|
| Script | `scripts/ops/regression_test.sh` |
| Count | 56 tests across 7 categories |
| Runtime | ~4 seconds |
| Default URL | `http://localhost:8080` |

### Running

```bash
# Against local development instance
bash scripts/ops/regression_test.sh http://localhost:8080

# Against production VM
bash scripts/ops/regression_test.sh http://<YOUR_WEBUI_IP>:8080

# Default (localhost)
bash scripts/ops/regression_test.sh
```

### Test Categories

#### 1. Connectivity (1 test)
Verifies the server is reachable and responding.

#### 2. Public Endpoints (5 tests)
Tests endpoints that should be accessible without authentication:
- `/health`
- `/api/auth/status`
- `/login`
- `/welcome`
- `/api/onboarding/status`

#### 3. Auth Gate (22 tests)
Verifies that all protected API endpoints return `302` (redirect to login) when accessed without authentication. Critically, none should return `500` (server error).

#### 4. Mutations (2 tests)
Tests POST endpoints return proper authentication responses (not server errors) when called without a session.

#### 5. Frontend Assets (1 composite test)
Parses `index.html`, extracts all referenced JS and CSS bundle paths, and verifies each one loads successfully with a `200` status.

#### 6. SPA Routes (15 tests)
Verifies that all client-side React routes serve the SPA shell (index.html) rather than returning 404. Routes tested include `/dashboard`, `/tickets`, `/settings`, `/approvals`, and others.

#### 7. Integration (4 tests)
End-to-end verification of:
- Full page load (HTML + all assets)
- Auth flow (unauthenticated redirect chain)
- Redirect chain correctness
- 500 error sweep (no endpoint returns a server error)

#### 8. Response Time (6 tests)
Verifies critical endpoints respond within 5 seconds:
- `/health`
- `/login`
- `/welcome`
- `/api/auth/status`
- `/api/onboarding/status`
- `/` (root)

## E2E Browser Tests

| Property | Value |
|----------|-------|
| Quick script | `ui/e2e-test.mjs` (Playwright) |
| Full suite | `ui/e2e/full_suite.cjs` (3,197 tests) |
| Requirements | Node.js + Playwright installed |

These tests exercise browser rendering, navigation, and user interactions through a real browser instance.

### Running

```bash
# Quick E2E
cd ui && node e2e-test.mjs

# Full suite
cd ui && node e2e/full_suite.cjs
```

## Post-Deploy Verification Checklist

Run this sequence after every deployment to `your-dashboard-vm`:

1. **Regression tests** -- confirms all endpoints work correctly:
   ```bash
   bash scripts/ops/regression_test.sh http://<YOUR_WEBUI_IP>:8080
   ```

2. **Service status** -- confirms the process is running:
   ```bash
   ssh your-dashboard-vm "systemctl --user status swe-dashboard"
   ```

3. **Metrics check** -- confirms metrics collection is working:
   ```bash
   ssh your-dashboard-vm "tail -1 ~/SWE-Squad/data/metrics/metrics-$(date -u +%Y-%m-%d).jsonl | python3 -m json.tool"
   ```

4. **Visual verification** -- open in browser:
   ```
   http://<YOUR_WEBUI_IP>:8080
   ```

5. **Error log check** -- no errors since deploy:
   ```bash
   ssh your-dashboard-vm "journalctl --user -u swe-dashboard --since '5 min ago' --no-pager | grep -i error"
   ```

## CI Gate (pre-merge)

All PRs must pass these three checks before merging:

| Check | Command | Criteria |
|-------|---------|----------|
| Unit tests | `python3 -m pytest tests/unit/ -q` | 0 failures |
| Regression tests | `bash scripts/ops/regression_test.sh` | 56/56 pass |
| Frontend build | `cd ui && npm run build` | Clean build, no errors |

Failure in any of these blocks the merge.
