#!/usr/bin/env bash
# Open a Telegram chat with the configured bot. The username is resolved live via
# the Bot API getMe call, so only TELEGRAM_BOT_TOKEN (env or ../.env) is needed.
# Silent no-op when the token is missing or getMe fails — never blocks run-all.
set -euo pipefail
cd "$(dirname "$0")/.."

for env_file in config/.env .env; do
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -f "$env_file" ]; then
    TELEGRAM_BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_file" | tail -1)"
  fi
done
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "open_bot: TELEGRAM_BOT_TOKEN not set; skipping" >&2
  exit 0
fi

username="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" |
  sed -n 's/.*"username":"\([^"]*\)".*/\1/p')" || username=""
if [ -z "$username" ]; then
  echo "open_bot: could not resolve bot username via getMe; skipping" >&2
  exit 0
fi

echo "open_bot: opening https://t.me/${username}"
if command -v open >/dev/null 2>&1; then # macOS: prefer the Telegram app deep link
  open "tg://resolve?domain=${username}" 2>/dev/null || open "https://t.me/${username}"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "https://t.me/${username}"
else
  echo "open_bot: open https://t.me/${username} manually"
fi
