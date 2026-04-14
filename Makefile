.PHONY: test lint lint-providers run daemon bootstrap sync-public \
        ui-install ui-dev ui-build ui-typecheck ui-preview dashboard

test:
	python3 -m pytest tests/ -v --tb=short

e2e:
	python3 -m pytest tests/e2e/ -v --tb=short

smoke:
	SWE_E2E_REAL=1 python3 -m pytest tests/e2e/ -v --tb=short -k "real"

lint:
	@command -v ruff >/dev/null 2>&1 && ruff check src/ scripts/ tests/ || echo "ruff not installed — skipping lint"

lint-providers:  ## Check for provider hardcoding violations
	python3 scripts/ops/lint_providers.py

run:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py

daemon:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py --daemon

bootstrap:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py --bootstrap -v

sync-public:
	bash scripts/ops/sync_public.sh

# ── React UI (ui/) ─────────────────────────────────────────────────────────

ui-install:  ## Install UI npm dependencies
	cd ui && npm install

ui-dev:  ## Start Vite dev server (proxies /api to :8080)
	cd ui && npm run dev

ui-build:  ## Production build → ui/dist/
	cd ui && npm run build

ui-typecheck:  ## TypeScript type-check (no emit)
	cd ui && npm run typecheck

ui-preview:  ## Preview production build locally
	cd ui && npm run preview

dashboard:  ## Start the Python dashboard server + Vite dev in parallel
	@echo "Starting dashboard server on :8080 and Vite on :5173..."
	@trap 'kill %1 %2' EXIT; \
	  python3 scripts/ops/dashboard_server.py & \
	  cd ui && npm run dev & \
	  wait
