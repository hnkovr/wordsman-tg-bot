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
