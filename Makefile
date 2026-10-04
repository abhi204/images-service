PYTHON ?= .venv/bin/python
ARCH ?=
LOCALSTACK_ENDPOINT ?= http://localhost:4567

.PHONY: help setup install lint test build up bootstrap compatibility integration recovery limits load demo verify down

help:
	@echo 'Targets: setup lint test build up bootstrap compatibility integration recovery limits load demo verify down'

setup install:
	python3.13 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:
	$(PYTHON) -m ruff check image_service scripts tests

test:
	$(PYTHON) -m pytest -q

build:
	$(PYTHON) scripts/build_lambda.py $(if $(ARCH),--arch $(ARCH))

up:
	docker compose up -d --wait

bootstrap: build
	$(PYTHON) scripts/bootstrap_local.py --endpoint-url $(LOCALSTACK_ENDPOINT) $(if $(ARCH),--arch $(ARCH))

compatibility:
	$(PYTHON) scripts/compatibility.py --endpoint $(LOCALSTACK_ENDPOINT)

integration:
	$(PYTHON) scripts/integration.py --config .local/api.json

recovery:
	$(PYTHON) scripts/verify_recovery.py

limits:
	$(PYTHON) scripts/verify_limits.py

load:
	$(PYTHON) scripts/load_test.py --config .local/api.json

demo:
	@$(PYTHON) -c 'import json; from pathlib import Path; print(json.loads(Path(".local/api.json").read_text())["api_url"].rstrip("/") + "/demo")'

verify: lint test compatibility bootstrap integration recovery limits

down:
	docker compose down
