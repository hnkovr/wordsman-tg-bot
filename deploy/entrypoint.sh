#!/usr/bin/env bash
# Cloud entrypoint: the API and the long-polling bot in one container.
# TG_BOT_PROCESSES=serve is the norm now — with TG_BOT_PUBLIC_URL set, `serve` IS the
# whole bot (webhook mode, docs/deploy.md); without it, `serve` is an API-only standby.
# "bot" adds long polling, which Telegram refuses while a webhook is registered — so at
# most one deployment anywhere may include it, and never the one owning the webhook.
set -euo pipefail
procs=",${TG_BOT_PROCESSES:-serve,bot},"
started=0
if [[ "$procs" == *,serve,* ]]; then
  tg-bot serve &
  started=1
fi
if [[ "$procs" == *,bot,* ]]; then
  tg-bot bot &
  started=1
fi
if ((!started)); then
  echo "TG_BOT_PROCESSES='${TG_BOT_PROCESSES:-}' selects neither serve nor bot" >&2
  exit 2
fi
# The first child to exit takes the container down; the platform restarts it.
wait -n
exit 1
