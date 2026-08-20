# wordsman-tg-bot ops. Common short commands live in the Makefile.

# Run API and bot together (Ctrl-C stops both), then open the Telegram chat
run-all:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run tg-bot serve &
    api_pid=$!
    trap 'kill "$api_pid" 2>/dev/null || true' EXIT
    (sleep 2 && bash scripts/open_bot.sh) &
    uv run tg-bot bot

# Open a Telegram chat with the configured bot (username resolved via getMe)
open-bot:
    bash scripts/open_bot.sh

# Health-chain check; names the first broken link. --e2e adds a live sample fetch.
doctor *args:
    bash scripts/doctor.sh {{ args }}

# Integration tests against the real wordsman checkout (repo-cache lane, no network)
test-integration *args:
    uv run pytest -m "integration and not network" --no-cov {{ args }}

# Same, plus the lanes that hit the live subtitle providers (slow, needs network)
test-integration-network *args:
    uv run pytest -m integration --no-cov {{ args }}

# Smoke-test the API against a running server (default port 8340)
smoke port="8340":
    curl -fsS "http://localhost:{{ port }}/healthz"
    curl -fsS -X POST "http://localhost:{{ port }}/api/v1/wordlists/movie" \
      -H 'Content-Type: application/json' -d '{"title": "Dune", "year": 2021}' \
      -o /tmp/dune-wordlists.zip && unzip -l /tmp/dune-wordlists.zip

# ── Cloud deploy (docs/deploy.md, wordsman#43) ─────────────────────────────────
# ONE BOT PER HOST (deploy/targets.yml): every target talks to its OWN Telegram bot, so
# deploying staging can never disturb production. --ha=false stays — a second machine on
# the same target would answer that bot's deliveries twice.

# List every deploy target with its bot, role and token variable
targets:
    @grep -E '^  [a-z]+:|^    (role|bot|token_var):' deploy/targets.yml | paste - - - - | column -t

# Ensure a target's own bot token exists and belongs to it (macOS dialog + @BotFather)
bot-token target="--all" *args:
    scripts/bot-token.sh {{ target }} {{ args }}

# Verify every target's token without prompting (doctor / CI lane)
bot-tokens-check:
    scripts/bot-token.sh --all --check-only

deploy-fly *args:
    scripts/bot-token.sh fly --check-only
    flyctl deploy --ha=false {{ args }}

fly-logs:
    flyctl logs -a wordsman-tg-bot --no-tail

fly-status:
    flyctl status -a wordsman-tg-bot

# Render = staging on @wordsman_render_bot. Pushes this target's env (its own bot token,
# its own public URL) and deploys. PUT /env-vars replaces the whole list, so render-env.py
# merges rather than sends — see the script's docstring.
deploy-render *args:
    scripts/bot-token.sh render --check-only
    scripts/render-env.py --target render {{ args }}

# Force a redeploy WITHOUT touching env vars (autodeploy from main already covers pushes).
render-redeploy service="srv-da0m9sdg1s2s73bvbbo0":
    #!/usr/bin/env bash
    set -euo pipefail
    key=$(grep -m1 '^RENDER_API_KEY=' ~/.ai/.env.secrets | cut -d= -f2-)
    curl -sf -X POST "https://api.render.com/v1/services/{{ service }}/deploys" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' -d '{}' | jq '{id, status}'
