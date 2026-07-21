.PHONY: install test lint format serve bot check

install:
	uv sync --group dev

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff check --fix . && uv run ruff format .

serve:
	uv run tg-bot serve

bot:
	uv run tg-bot bot

check: lint test
