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
| `POST /api/v1/wordlists/movie` | `{"title": "Dune", "year": 2021, "user_id": 42}` | ZIP of wordlists (404 = no subtitles) |
| `POST /api/v1/wordlists/document` | multipart `file` (+ optional `user_id`) | ZIP of wordlists |

`user_id` is optional; when present the request uses that user's saved preferences and
an isolated work directory.

## Multi-user preferences

Each Telegram user gets their own settings, stored in a shared SQLite DB
(`db_path`, default `data/tg_bot.sqlite3`) that the bot writes and the API reads. Users
manage them from the bot's command menu:

- **/settings** — an inline-keyboard menu to set the minimum CEFR **level**, the **max
  words** per list, and which export **formats** to include (toggle per format)
- **/reset** — restore the global defaults
- **/help** — usage

Unset preferences inherit the global `config.yml` defaults (the DB stores only explicit
overrides). Each user's generation runs in its own `work/<user_id>/` namespace, so
concurrent requests for the same title don't collide.

## Quickstart

```bash
uv sync --group dev
bash config/env-render.sh      # renders config/.env; then fill TELEGRAM_BOT_TOKEN
uv run tg-bot serve            # terminal 1: FastAPI on :8340
uv run tg-bot bot              # terminal 2: Telegram polling
just run-all                   # or: both at once + opens the Telegram chat
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
