PYTHON ?= python3.14
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test ui check dev-api dev-ui up down reset demo smoke clean

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
# --app-dir src rather than relying on the editable install: in an
# iCloud-synced folder macOS marks everything inside .venv hidden, and
# Python 3.13+ skips hidden .pth files, so `import logscope` would fail.
dev-api:
	$(BIN)/uvicorn logscope.app:app --app-dir src --reload

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

# Load synthetic demo events (shaped like Gatekeeper's and SurgiServe's
# logs, the last six hours) into the stack `make up` started.
demo:
	python3 scripts/demo_data.py

# Check a fresh stack over HTTP from outside. It runs as its own Compose
# project, with its own volume and port, so it starts empty every time and
# never touches the data of `make up`; its volume is deleted afterwards.
SMOKE := COMPOSE_PROJECT_NAME=logscope-smoke LOGSCOPE_PORT=8201 BASE_URL=http://localhost:8201

smoke:
	$(SMOKE) docker compose up --build --detach --wait
	$(SMOKE) ./scripts/smoke.sh || ($(SMOKE) docker compose logs; $(SMOKE) docker compose down --volumes; exit 1)
	$(SMOKE) docker compose down --volumes

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache build *.egg-info src/*.egg-info ui/node_modules ui/dist
