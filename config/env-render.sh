#!/usr/bin/env bash
# Render ../.env from .env.template: ${VAR:-default} expressions are evaluated
# against the current environment, so exported secrets flow in and everything
# else falls back to the template defaults. Refuses to overwrite an existing .env.
set -euo pipefail
cd "$(dirname "$0")"
out=".env" # config/.env — the canonical secrets file (root .env is a fallback)
if [ -f "$out" ]; then
  echo "env-render: $out already exists; not overwriting" >&2
  exit 1
fi
while IFS= read -r line; do
  case "$line" in
    '' | '#'*) printf '%s\n' "$line" ;;
    *) eval "printf '%s\n' \"$line\"" ;;
  esac
done <.env.template >"$out"
echo "env-render: wrote $out"
