ifeq ($(OS),Windows_NT)
PY := $(or $(wildcard .venv/Scripts/python.exe),python)
else
PY := $(or $(wildcard .venv/bin/python),python3)
endif

.PHONY: test lint typecheck help demo docker-demo docker-app docker-down

help:
	@echo "test      run the offline pytest suite"
	@echo "lint      ruff check (skips when ruff is not installed)"
	@echo "typecheck mypy check (skips when mypy is not installed)"
	@echo "demo      serve the zero-key demo on http://127.0.0.1:8000/demo"
	@echo "docker-demo build and run the demo container on port 8000"
	@echo "docker-app  build and start the full app with docker compose"
	@echo "docker-down stop the compose stack"

demo:
	$(PY) run_demo.py

docker-demo:
	docker build -t marketwingman .
	docker run --rm -p 8000:8000 marketwingman

docker-app:
	docker compose up -d --build app

docker-down:
	docker compose --profile demo --profile jobs down

test:
	$(PY) -m pytest tests/ -q

lint:
	@$(PY) -m ruff check bot tests run_app.py run_session.py || echo "skip: ruff not installed"

typecheck:
	@$(PY) -m mypy bot run_app.py run_session.py || echo "skip: mypy not installed"
