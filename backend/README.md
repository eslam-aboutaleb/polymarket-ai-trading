# Backend Quick Notes

## Run Locally

1. Install dependencies: `uv sync`
2. Apply DB migrations: `uv run alembic upgrade head`
3. Start API: `uv run uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload`

## Tests

Run backend tests with:

`PYTHONPATH=. uv run pytest -q`
