#!/usr/bin/env python3
"""Push this target's env vars to the Render service and trigger a deploy.

Render's `PUT /v1/services/{id}/env-vars` REPLACES the whole list rather than merging it,
so anything not resent is deleted — send only the new var and the service loses its bot
token. This script always reads the current list first and merges, which is the whole
reason it exists instead of a curl one-liner.

The bot token comes from the target's OWN variable (deploy/targets.yml), resolved through
every secret store by ~/.ai/skills/_scripts/secrets/find-secret.sh, so staging deploys
carry the staging bot and can never disturb production.

Usage: scripts/render-env.py [--target render] [--no-deploy] [--show]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGETS = REPO / "deploy" / "targets.yml"
FIND_SECRET = Path.home() / ".ai/skills/_scripts/secrets/find-secret.sh"
API = "https://api.render.com/v1"


def find_secret(var: str) -> str:
    """Resolve a secret from any store on this machine (env first, so CI can override)."""
    if os.environ.get(var):
        return os.environ[var]
    if not FIND_SECRET.exists():
        sys.exit(f"{var} is not in the environment and {FIND_SECRET} is missing")
    got = subprocess.run([str(FIND_SECRET), var, "--quiet"], capture_output=True, text=True)
    if got.returncode or not got.stdout:
        sys.exit(f"{var} not found in any secret store — run: scripts/bot-token.sh <target>")
    return got.stdout.strip()


def load_target(name: str) -> dict[str, str]:
    text = TARGETS.read_text(encoding="utf-8")
    try:
        import yaml

        target = yaml.safe_load(text)["targets"].get(name)
    except ImportError:  # PyYAML is optional; the file is flat enough to scan
        target, cur = None, None
        for line in text.splitlines():
            if m := re.match(r"^  ([a-z0-9_-]+):\s*$", line):
                cur = m.group(1)
                if cur == name:
                    target = {}
            elif target is not None and cur == name:
                if m := re.match(r"^    ([a-z_]+):\s*(.*)$", line):
                    target[m.group(1)] = m.group(2).strip().strip('"')
                elif not line.startswith("    "):
                    break
    if not target:
        sys.exit(f"unknown target {name!r} in {TARGETS}")
    return target


def api(method: str, path: str, body: object = None) -> object:
    """Call the Render API through curl.

    Not urllib: the python.org macOS builds ship without a CA store, so urllib raises
    CERTIFICATE_VERIFY_FAILED on this very machine while every other script here (all
    curl-based) works. The token goes in via a header on stdin-free argv only because
    curl needs it there; it is never echoed.
    """
    cmd = [
        "curl",
        "-sS",
        "-X",
        method,
        f"{API}{path}",
        "-w",
        "\n%{http_code}",
        "-H",
        f"Authorization: Bearer {find_secret('RENDER_API_KEY')}",
    ]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"render {method} {path}: curl failed — {out.stderr[:300]}")
    raw, _, status = out.stdout.rpartition("\n")
    if not status.startswith("2"):
        sys.exit(f"render {method} {path} → HTTP {status}: {raw[:300]}")
    return json.loads(raw) if raw.strip() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="render")
    ap.add_argument("--no-deploy", action="store_true", help="update env vars only")
    ap.add_argument("--show", action="store_true", help="print the key names and exit")
    ap.add_argument(
        "--token-var",
        default=None,
        help=(
            "serve a DIFFERENT bot than this target owns — a deliberate failover, e.g. "
            "--token-var WORDSMAN_TG_BOT_TOKEN to run production from here while its own "
            "host is down. Breaks one-bot-per-host for as long as it is set, so the two "
            "deployments must never both run with it."
        ),
    )
    args = ap.parse_args()

    target = load_target(args.target)
    service = target.get("service") or sys.exit(f"target {args.target} has no service id")
    current = {
        e["envVar"]["key"]: e["envVar"]["value"]
        for e in api("GET", f"/services/{service}/env-vars?limit=100")
    }
    if args.show:
        print("\n".join(sorted(current)))
        return

    # This host's own bot, its own public URL: one bot per host means no webhook contention.
    # --token-var overrides that on purpose, for a failover.
    token_var = args.token_var or target["token_var"]
    current["TELEGRAM_BOT_TOKEN"] = find_secret(token_var)
    current["TG_BOT_PUBLIC_URL"] = target["url"]
    current["TG_BOT_WEBHOOK_SECRET"] = find_secret("TG_BOT_WEBHOOK_SECRET")
    current.setdefault("TG_BOT_PORT", "10000")  # Render's canonical web port
    current.setdefault("TG_BOT_SERVICE_NOTIFY", "false")

    result = api(
        "PUT",
        f"/services/{service}/env-vars",
        [{"key": k, "value": v} for k, v in sorted(current.items())],
    )
    print(f"{args.target}: env-vars =", sorted(e["envVar"]["key"] for e in result))
    serving = target["bot"] if token_var == target["token_var"] else f"NOT its own bot — {token_var}"
    print(f"{args.target}: bot =", serving, "via", token_var)
    if args.no_deploy:
        return
    deploy = api("POST", f"/services/{service}/deploys", {"clearCache": "do_not_clear"})
    print(f"{args.target}: deploy {deploy['id']} {deploy['status']}")


if __name__ == "__main__":
    main()
