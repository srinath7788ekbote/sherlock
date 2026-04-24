.PHONY: install run test test-fast test-synthetics test-dependencies test-deeplinks test-domain-tools test-session-memory test-structured-output test-frustration test-asb test-cov lint format logs audit connect relearn relearn-all relearn-list cli clean

install:
	pip install -e ".[dev]"

run:
	python main.py

test:
	pytest tests/ -v -n auto

test-fast:
	pytest tests/ -x -q -n auto

test-synthetics:
	pytest tests/test_synthetics.py -v

test-dependencies:
	pytest tests/test_dependencies_tool.py tests/test_dependency_graph.py tests/test_graph_builder.py -v

test-deeplinks:
	pytest tests/test_deeplinks.py -v

test-domain-tools:
	pytest tests/test_golden_signals.py tests/test_k8s.py tests/test_apm.py tests/test_logs.py tests/test_alerts.py tests/test_synthetics.py -v

test-session-memory:
	pytest tests/test_session_memory.py -v

test-structured-output:
	pytest tests/test_structured_output.py -v

test-frustration:
	pytest tests/test_frustration_detection.py -v

test-asb:
	pytest tests/test_asb_intelligence.py -v

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
	python scripts/validate_connection.py

relearn:
ifdef PROFILE
	python scripts/relearn.py --profile $(PROFILE)
else
	python scripts/relearn.py
endif

relearn-all:
	python scripts/relearn.py

relearn-list:
	python scripts/relearn.py --list

cli:
	python scripts/cli.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
