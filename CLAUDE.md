# Claude Notes

See [README.md](README.md) for usage and [AGENTS.md](AGENTS.md) for guardrails.

- Pipeline orchestration lives in [src/tg_bot/pipeline.py](src/tg_bot/pipeline.py);
  it shells out to the parent wordsman checkout (`main.py subtitle-dict`,
  `scripts/fetch_srt.sh`) and never re-implements the pipeline.
- Bot handlers ([src/tg_bot/bot.py](src/tg_bot/bot.py)) stay thin: Telegram I/O plus
  HTTP calls to the FastAPI service; all logic testable without aiogram objects.
