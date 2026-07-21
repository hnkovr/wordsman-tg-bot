# wordsman-tg-bot ops. Common short commands live in the Makefile.

# Run API and bot together (Ctrl-C stops both)
run-all:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run tg-bot serve &
    api_pid=$!
    trap 'kill "$api_pid" 2>/dev/null || true' EXIT
    uv run tg-bot bot

# Smoke-test the API against a running server (default port 8340)
smoke port="8340":
    curl -fsS "http://localhost:{{ port }}/healthz"
    curl -fsS -X POST "http://localhost:{{ port }}/api/v1/wordlists/movie" \
      -H 'Content-Type: application/json' -d '{"title": "Dune", "year": 2021}' \
      -o /tmp/dune-wordlists.zip && unzip -l /tmp/dune-wordlists.zip
