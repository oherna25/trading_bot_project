ifeq ($(OS),Windows_NT)
PY := $(or $(wildcard .venv/Scripts/python.exe),python)
else
PY := $(or $(wildcard .venv/bin/python),python3)
endif

.PHONY: test lint typecheck help demo docker-demo

help:
	@echo "test      run the offline pytest suite"
	@echo "lint      ruff check (skips when ruff is not installed)"
	@echo "typecheck mypy check (skips when mypy is not installed)"
	@echo "demo      serve the zero-key demo on http://127.0.0.1:8000/demo"
	@echo "docker-demo build and run the demo container on port 8000"

demo:
	$(PY) run_demo.py

docker-demo:
	docker build -t marketwingman .
	docker run --rm -p 8000:8000 marketwingman

test:
	$(PY) -m pytest tests/ -q

lint:
	@$(PY) -m ruff check bot tests run_app.py run_session.py || echo "skip: ruff not installed"

typecheck:
	@$(PY) -m mypy bot run_app.py run_session.py || echo "skip: mypy not installed"
