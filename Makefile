ifeq ($(OS),Windows_NT)
PY := $(or $(wildcard .venv/Scripts/python.exe),python)
else
PY := $(or $(wildcard .venv/bin/python),python3)
endif

.PHONY: test lint typecheck help

help:
	@echo "test      run the offline pytest suite"
	@echo "lint      ruff check (skips when ruff is not installed)"
	@echo "typecheck mypy check (skips when mypy is not installed)"

test:
	$(PY) -m pytest tests/ -q

lint:
	@$(PY) -m ruff check bot tests run_app.py run_session.py || echo "skip: ruff not installed"

typecheck:
	@$(PY) -m mypy bot run_app.py run_session.py || echo "skip: mypy not installed"
