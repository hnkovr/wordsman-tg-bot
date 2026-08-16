#!/usr/bin/env bash
# Cloud entrypoint: the API and the long-polling bot in one container.
# TG_BOT_PROCESSES=serve → API-only standby. Telegram allows exactly ONE getUpdates
# consumer per token, so only one deployment anywhere may include "bot".
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
