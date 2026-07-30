# Agents

- This repo is an autonomous wordsman subproduct (submodule `subproducts/tg-bot` in
  [hnkovr/wordsman](https://github.com/hnkovr/wordsman)). Work here, commit + push
  here, then bump the pin in the parent — never edit through the parent tree.
- Keep tests hermetic: the fake wordsman root in `tests/conftest.py` replaces
  network, Telegram, and the real pipeline. Never spawn real fetches in CI.
- Secrets: `TELEGRAM_BOT_TOKEN` lives only in `.env` (gitignored); the template in
  `config/.env.template` must keep blank values.
- Mirror `wordsman-srt-api` conventions: pydantic-settings, loguru, click CLI,
  FastAPI `create_app()`, hatchling `src/` layout, pytest coverage ≥ 85%.

<!-- tg-bot-service-chat:begin (auto-upsert via scripts/upsert_docs.py) -->

## Service chat & secrets

- Bot activity is mirrored into the service chat configured by `TG_BOT_SERVICE_CHAT_ID`
  (+ optional `TG_BOT_SERVICE_THREAD_ID`); end users always interact with the bot directly.
- `ServiceNotifier.send()` must never raise into a handler — notification failures are
  logged and swallowed by design; keep it that way when adding call sites.
- Secrets (`TELEGRAM_BOT_TOKEN`, chat ids) live only in the gitignored `config/.env`,
  rendered from `config/.env.template` via `config/env-render.sh`. The template keeps blank
  or `${VAR:-default}` forms — a literal value there is a leak.

<!-- tg-bot-service-chat:end -->
