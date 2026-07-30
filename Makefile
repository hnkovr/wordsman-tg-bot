.PHONY: install test test-integration lint format serve bot run-all all tg-bot-all doctor check

install:
	uv sync --group dev

# Run API + bot together and open the bot chat. `all`/`tg-bot-all` are aliases so the
# same command works whether you're in this subproduct or the parent repo root.
run-all all tg-bot-all: ## Run API + bot together, then open the bot chat (Ctrl-C stops both)
	just run-all

doctor: ## Health-chain check: names the first broken link (config→root→provider→API)
	bash scripts/doctor.sh

test:
	uv run pytest

test-integration: ## Real wordsman checkout + repo cache, no network (needs `git lfs pull` there)
	just test-integration

lint:
	uv run ruff check .

format:
	uv run ruff check --fix . && uv run ruff format .

serve:
	uv run tg-bot serve

bot:
	uv run tg-bot bot

check: lint test
