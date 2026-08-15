.PHONY: install check test lint typecheck scan migrate serve

install:
	python3.12 -m venv .venv
	.venv/bin/pip install -e '.[dev,telegram,storage,security,webapp]'

check: lint typecheck test

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .

typecheck:
	.venv/bin/mypy src

scan:
	.venv/bin/pumplens scan

migrate:
	.venv/bin/alembic upgrade head

serve:
	.venv/bin/pumplens serve
