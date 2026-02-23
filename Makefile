.PHONY: install run test test-fast test-synthetics lint format logs audit connect cli clean

install:
	pip install -e ".[dev]"

run:
	python main.py

test:
	pytest tests/ -v

test-fast:
	pytest tests/ -x -q

test-synthetics:
	pytest tests/test_synthetics.py -v

lint:
	ruff check . && mypy .

format:
	ruff format .

logs:
	tail -f ~/.sherlock/logs/sherlock.log | python -m json.tool

audit:
	tail -f ~/.sherlock/logs/audit.log | python -m json.tool

connect:
	python scripts/test_connection.py

cli:
	python scripts/cli.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
