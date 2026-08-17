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

# ── Cloud deploy (README "Cloud deploy", wordsman#43) ──────────────────────────
# Fly = ACTIVE deployment. --ha=false keeps the getUpdates singleton: one machine,
# and never enable the `bot` process while another poller holds the same token.
deploy-fly *args:
    flyctl deploy --ha=false {{ args }}

fly-logs:
    flyctl logs -a wordsman-tg-bot --no-tail

fly-status:
    flyctl status -a wordsman-tg-bot

# Render = API-only standby; autodeploys from main. This forces a manual redeploy.
render-redeploy service="srv-da0m9sdg1s2s73bvbbo0":
    #!/usr/bin/env bash
    set -euo pipefail
    key=$(grep -m1 '^RENDER_API_KEY=' ~/.ai/.env.secrets | cut -d= -f2-)
    curl -sf -X POST "https://api.render.com/v1/services/{{ service }}/deploys" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' -d '{}' | jq '{id, status}'
