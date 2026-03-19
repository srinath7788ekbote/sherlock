.PHONY: install run test test-fast test-synthetics test-investigate test-dependencies test-discovery test-deeplinks test-cov lint format logs audit connect relearn cli clean

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

test-investigate:
	pytest tests/test_investigate.py tests/test_discovery.py tests/test_query_builder.py -v

test-dependencies:
	pytest tests/test_dependencies_tool.py tests/test_dependency_graph.py tests/test_graph_builder.py -v

test-discovery:
	pytest tests/test_discovery.py -v

test-deeplinks:
	pytest tests/test_deeplinks.py -v

test-cov:
	pytest tests/ -v --cov=. --cov-report=html --cov-report=term

lint:
	ruff check . && mypy .

format:
	ruff format .

logs:
	tail -f .sherlock/logs/sherlock.log | python -m json.tool

audit:
	tail -f .sherlock/logs/audit.log | python -m json.tool

connect:
	python scripts/test_connection.py

relearn:
	python scripts/cli.py --profile DFIN_AD --tool learn_account

cli:
	python scripts/cli.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
