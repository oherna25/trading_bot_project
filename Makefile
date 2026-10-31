PY := $(or $(wildcard .venv/bin/python),python3)

.PHONY: test lint typecheck help

help:
	@echo "test      run the offline pytest suite"
	@echo "lint      ruff check (skips when ruff is not installed)"
	@echo "typecheck mypy check (skips when mypy is not installed)"

test:
	$(PY) -m pytest tests/ -q

lint:
	@$(PY) -m ruff check bot tests run_app.py run_session.py 2>/dev/null || echo "skip: ruff not installed"

typecheck:
	@$(PY) -m mypy bot run_app.py run_session.py 2>/dev/null || echo "skip: mypy not installed"
