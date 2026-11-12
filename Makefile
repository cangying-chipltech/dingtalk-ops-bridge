.PHONY: test e2e run monitor-once build up down validate

test:
	./scripts/run-tests.sh

e2e:
	./scripts/run-e2e.sh

run:
	./scripts/agent-router.sh help

monitor-once:
	PYTHONPATH=src python3 -m dingtalk_ops_bridge monitor --once --dry-run

validate:
	PYTHONPATH=src python3 -m dingtalk_ops_bridge validate

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down
