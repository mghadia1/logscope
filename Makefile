PYTHON ?= python3.14
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test ui check dev-api dev-ui up down reset smoke clean

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/python -m pip install -e '.[dev]'
	cd ui && npm ci

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	cd ui && npm run lint

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/python -m pytest -q
	cd ui && npm test

# Build the UI into ui/dist, which the API serves at /.
ui:
	cd ui && npm run build

# Everything CI runs before the Docker job.
check: lint typecheck test ui

# Development: the API on :8000 and Vite on :5173 (which forwards /api).
dev-api:
	$(BIN)/uvicorn logscope.app:app --reload

dev-ui:
	cd ui && npm run dev

# --wait blocks until the container's HEALTHCHECK reports healthy.
up:
	docker compose up --build --detach --wait

down:
	docker compose down

# Also delete the data volume.
reset:
	docker compose down --volumes

# Bring the stack up, check it over HTTP from outside, tear it down.
smoke: up
	./scripts/smoke.sh
	$(MAKE) down

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache build *.egg-info src/*.egg-info ui/node_modules ui/dist
