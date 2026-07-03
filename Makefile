.PHONY: install redis run-api run-worker test test-contract test-unit new-domain docker-build

install:
	pip install -e . --group dev

redis:
	docker run --rm -p 6379:6379 --name dda-redis redis:7-alpine

run-api:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

run-worker:
	arq app.worker.main.WorkerSettings

test:
	pytest -q

test-contract:
	pytest -q tests/contract

test-unit:
	pytest -q tests/unit

# Scaffold a new domain adapter: make new-domain NAME=confluence
new-domain:
	python scripts/new_domain.py $(NAME)

docker-build:
	docker build -t domain-deep-agent:local .
