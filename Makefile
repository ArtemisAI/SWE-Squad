.PHONY: test lint run daemon bootstrap sync-public

test:
	python3 -m pytest tests/ -v --tb=short

e2e:
	python3 -m pytest tests/e2e/ -v --tb=short

smoke:
	SWE_E2E_REAL=1 python3 -m pytest tests/e2e/ -v --tb=short -k "real"

lint:
	@command -v ruff >/dev/null 2>&1 && ruff check src/ scripts/ tests/ || echo "ruff not installed — skipping lint"

run:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py

daemon:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py --daemon

bootstrap:
	SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py --bootstrap -v

sync-public:
	bash scripts/ops/sync_public.sh
