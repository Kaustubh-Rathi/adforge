.PHONY: help install dev lint test test-unit test-integration run-api run-dashboard run-mcp seed db-up db-down db-reset docker-up docker-down docker-logs

help:
	@echo "AdForge — available targets:"
	@echo "  make install          Install dependencies (dev)"
	@echo "  make dev              Install + seed local dev env"
	@echo "  make lint             Run ruff + mypy"
	@echo "  make test             Run full test suite"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests (needs Postgres + Redis)"
	@echo "  make db-up            Start Postgres + Redis via docker-compose"
	@echo "  make db-reset         Drop + recreate dev database"
	@echo "  make seed             Seed example campaigns + sample KPI data"
	@echo "  make run-api          Start FastAPI on :8000"
	@echo "  make run-dashboard    Start Streamlit on :8501"
	@echo "  make run-mcp          Start a single MCP server (NAME=research)"
	@echo "  make docker-up        Boot the whole stack via docker-compose"
	@echo "  make docker-down      Tear the whole stack down"

install:
	pip install -e ".[dev]"

dev: install db-up seed
	@echo "Dev environment ready. Run 'make run-api' and 'make run-dashboard' in two terminals."

lint:
	ruff check adforge tests
	ruff format --check adforge tests
	mypy adforge

test:
	pytest

test-unit:
	pytest -m "not integration"

test-integration:
	pytest -m "integration"

db-up:
	docker compose up -d postgres redis
	@echo "Waiting for services..."
	@sleep 5

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d postgres redis
	@sleep 5
	@echo "Database reset."

seed:
	python -m adforge.storage.seed

run-api:
	uvicorn adforge.api.main:app --reload --port 8000

run-dashboard:
	streamlit run adforge/dashboard/app.py --server.port=8501

run-mcp:
	python -m adforge.mcp_servers.runner $(NAME)

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=100
