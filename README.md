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

## Search (/ru, /ru_subs, /ru_audio, /en_audio, /orig_audio)

Finds **subtitles and audio tracks** for a movie and replies with one report of four
sections (Telegram commands allow only `a-z0-9_` — hence the underscores). Subtitles are
Russian-only; audio can be Russian, English, or the release's original track:

- **/ru <movie>** — everything; **/ru_subs** — subtitles only; **/ru_audio** — audio only
- **/en_audio <movie>** — English audio tracks; **/orig_audio <movie>** — the original
  (untranslated) track, whatever language the release was shot in
- 📀 *already on disk* — a second wordsman checkout's stdlib `wordsman.search` module
  scans local media (`search_wordsman_root`; unset → leg disabled). `--lang` is passed
  only for non-Russian audio, so a checkout predating the flag still serves `/ru*`
- 🌐 *online subtitles* — `subproducts/srt-search` with `SRT_SEARCH_LANGUAGE=ru`
  (providers from `ru_subs_providers`, default `subtitlecat`)
- 🔊 *online audio* — `subproducts/audio-search` `find --langs <ru|en> --json`
  (feat-branch subproduct; absent → the section explains why). `/orig_audio` runs that
  search **without** a language filter — audio-search has no "original" language — and
  the section header says so; the original-track heuristics live in the local leg
  (`main.py search-audio --lang original`)
- 🔗 *where to look manually* — the `dual_subtitle_sources` / `audio_sources`
  catalogs rendered as search links. **Torrent entries are links-only by policy:
  the bot renders tracker-search URLs and never scrapes or downloads from them.**

`/settings → Search language → RU` makes plain-text messages run this RU search
instead of the EN wordlist flow (`/reset` or `Default (inherit)` switches back).
Every leg degrades to an explanation instead of failing — a dead provider or a
missing subproduct never hides the other sections.

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
`except_list`, `top`, `min_level`, `max_document_mb`, timeouts; RU search:
`search_wordsman_root`, `ru_scan_dirs`, `ru_subs_providers`, `ru_limit`,
`ru_search_timeout`, `ru_subs_sources_file`/`ru_audio_sources_file`.

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

## Cloud deploy (Fly.io + Render)

`deploy/Dockerfile.cloud` is a self-contained image: it clones the parent wordsman
checkout (with the srt-search subproduct) at build time, so no volume mounts are
needed.

The cloud deployment runs in **webhook mode**: Telegram POSTs each update to
`TG_BOT_PUBLIC_URL` + `TG_BOT_WEBHOOK_PATH`, which `tg-bot serve` handles itself. There
is no poller, so nothing has to stay awake — the delivery wakes a stopped Fly machine.
Long polling (`tg-bot bot`) remains the local-development transport.

**One bot per host** ([`deploy/targets.yml`](deploy/targets.yml)): Fly is production on
@wordsman_bot, Render is staging on @wordsman_render_bot. A token has exactly one webhook
URL, so hosts sharing a token would have to negotiate ownership; separate tokens simply
cannot contend, and staging can be redeployed at will.

```bash
just targets            # role, bot and token variable per host
just bot-token render   # ensure that bot's token — searches every store, asks only if it must
just deploy-render      # staging (its own bot), just deploy-fly for production
```

`scripts/bot-token.sh` looks in the process env, `~/.ai/.env.secrets`, project env files,
the macOS keychain and Bitwarden before asking anyone. When it must ask, it opens
@BotFather with instructions that differ depending on whether the bot already exists, and
checks whatever you paste against `getMe` immediately — a token belonging to a *different*
bot is rejected, not saved.

**Within one bot** the transport is still single-valued: only a deployment with
`TG_BOT_PUBLIC_URL` set is eligible, and it claims the webhook on startup only when it is
unset or already its own, never from a live owner. `/healthz` reports which:
`webhook` · `standby` · `off`.

```bash
# Fly (active)
fly deploy --ha=false                       # --ha=false: one machine answers deliveries
printf 'TELEGRAM_BOT_TOKEN=%s' "$WORDSMAN_TG_BOT_TOKEN" | fly secrets import
fly secrets set TG_BOT_WEBHOOK_SECRET="$(openssl rand -hex 16)" -a wordsman-tg-bot

tg-bot webhook info      # what Telegram has registered (url, pending, last error)
tg-bot webhook set       # re-point Telegram at TG_BOT_PUBLIC_URL
tg-bot webhook delete    # hand the token back to long polling

# Render (standby): render.yaml Blueprint, or service created via the Render API;
# set TELEGRAM_BOT_TOKEN in the dashboard (sync: false keeps it out of the repo).
```

Details, verification recipe and the trial-plan caveats: [docs/deploy.md](docs/deploy.md).

State is ephemeral on both platforms: `data/tg_bot.sqlite3` (per-user prefs) resets
on every deploy. Attach a Fly volume if prefs must survive restarts.
