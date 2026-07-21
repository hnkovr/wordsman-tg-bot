# wordsman-tg-bot

Telegram bot + FastAPI service that turns a **movie name** or a **document**
(`.pdf`, `.html`, `.srt`, `.vtt`, `.txt`, `.md`) into
[wordsman](https://github.com/hnkovr/wordsman) vocabulary wordlists and sends them
back as a ZIP.

A wordsman subproduct, pinned as git submodule at `subproducts/tg-bot`.

## Architecture

```
Telegram user ⇄ bot (aiogram v3, polling)
                 │ httpx
                 ▼
     FastAPI service (tg_bot.api, :8340)
                 │ subprocess
                 ├── bash $WORDSMAN_ROOT/scripts/fetch_srt.sh    movie → SRT
                 └── python $WORDSMAN_ROOT/main.py subtitle-dict input → wordlists
```

- Wordlists are generated in **every supported wordsman format except a short
  exception list** (`formats_exclude` in [config/config.yml](config/config.yml);
  default: the sparse `sparsed-yaml`, `sparsed-json` variants).
- Words in [config/except-words.txt](config/except-words.txt) (and all their forms)
  are dropped from every list via wordsman's `--except-list`.

## API

| Endpoint | Body | Returns |
| --- | --- | --- |
| `GET /healthz` | — | service status |
| `POST /api/v1/wordlists/movie` | `{"title": "Dune", "year": 2021}` | ZIP of wordlists (404 = no subtitles) |
| `POST /api/v1/wordlists/document` | multipart `file` | ZIP of wordlists |

## Quickstart

```bash
uv sync --group dev
cp config/.env.template .env   # fill TELEGRAM_BOT_TOKEN
uv run tg-bot serve            # terminal 1: FastAPI on :8340
uv run tg-bot bot              # terminal 2: Telegram polling
```

Run from the wordsman checkout (`subproducts/tg-bot`) and `WORDSMAN_ROOT` is
auto-detected; standalone, set `TG_BOT_WORDSMAN_ROOT=/path/to/wordsman`.

## Configuration

All keys in [config/config.yml](config/config.yml), overridable via `TG_BOT_*` env
vars (`TELEGRAM_BOT_TOKEN` is also accepted unprefixed). Key knobs: `formats_exclude`,
`except_list`, `top`, `min_level`, `max_document_mb`, timeouts.

## Tests

```bash
uv run pytest          # hermetic: fake wordsman root, no network/Telegram
uv run ruff check .
```

## Docker

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Mounts the parent wordsman checkout at `/wordsman` and runs `api` + `bot` services.
