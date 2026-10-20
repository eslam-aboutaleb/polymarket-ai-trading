# Contract consumed by the SHA-pinned shared integration workflow.
# Both URLs must name disposable CI services, never an application database.
export ENVIRONMENT := test
export JWT_SECRET_KEY := ci-only-key-never-used-for-production-signing
export DATABASE_URL = $(CI_POSTGRES_URL)
export TEST_DATABASE_URL = $(CI_POSTGRES_URL)
export REDIS_URL = $(CI_REDIS_URL)

.PHONY: ci-install ci-migrate ci-integration

ci-install:
	uv sync --all-groups --frozen

ci-migrate:
	@test -n "$$CI_POSTGRES_URL" || { echo "CI_POSTGRES_URL must name a disposable database"; exit 1; }
	uv run --frozen alembic upgrade head
	uv run --frozen alembic downgrade 20261016_0021_drop_redundant_pk_indexes
	uv run --frozen alembic upgrade head

ci-integration:
	@test -n "$$TEST_DATABASE_URL" || { echo "TEST_DATABASE_URL is required; refusing to skip PostgreSQL tests"; exit 1; }
	uv run --frozen pytest --no-cov tests/security tests/trading tests/integration/test_trading_halt_postgres.py --junitxml=test-results/trading-safety.xml
