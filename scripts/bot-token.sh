#!/usr/bin/env bash
# bot-token.sh — make sure a deploy target's OWN Telegram bot token is present and valid.
#
# Reads deploy/targets.yml for the target's bot username and token variable, then hands
# the work to the machine-global helper, which searches every secret store first and only
# asks the human (macOS dialog + @BotFather) when the machine genuinely cannot answer.
#
# Kept as a thin wrapper on purpose: the search/ask/verify logic is shared with every
# other project on this machine, and a copy here would drift the moment a store is added.
#
# Usage: scripts/bot-token.sh <target> [--check-only]
#        scripts/bot-token.sh --all [--check-only]
# Exit: 0 all requested targets have a verified token · non-zero otherwise
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

HELPER="$HOME/.ai/skills/_scripts/integrations/telegram/ensure-tg-bot.sh"
TARGETS="deploy/targets.yml"

[[ $# -gt 0 ]] || {
  echo "usage: scripts/bot-token.sh <target|--all> [--check-only]" >&2
  exit 4
}
TARGET="$1"
shift

field() { # <target> <key>
  python3 -c "
import sys, pathlib, re
name, key = sys.argv[1], sys.argv[2]
try:
    import yaml
    data = yaml.safe_load(pathlib.Path('$TARGETS').read_text())['targets']
except ImportError:  # stay usable without PyYAML — the file is flat enough to scan
    data, cur = {}, None
    for line in pathlib.Path('$TARGETS').read_text().splitlines():
        m = re.match(r'^  ([a-z0-9_-]+):\s*\$', line)
        if m:
            cur = m.group(1); data[cur] = {}
        elif cur and (m := re.match(r'^    ([a-z_]+):\s*(.*)\$', line)):
            data[cur][m.group(1)] = m.group(2).strip().strip('\"')
print(data.get(name, {}).get(key, ''))
" "$1" "$2"
}

names() {
  python3 -c "
import pathlib, re
print(' '.join(re.findall(r'^  ([a-z0-9_-]+):\s*\$', pathlib.Path('$TARGETS').read_text(), re.M)))
"
}

ensure_one() {
  local t="$1" bot var title role
  shift  # the rest are pass-through flags for the helper (e.g. --check-only)
  bot="$(field "$t" bot)"
  var="$(field "$t" token_var)"
  title="$(field "$t" title)"
  role="$(field "$t" role)"
  [[ -n "$bot" && -n "$var" ]] || {
    echo "bot-token: unknown target '$t' (have: $(names))" >&2
    return 4
  }
  echo "── $t ($role): $bot → \$$var"
  [[ -x "$HELPER" ]] || {
    echo "   helper missing: $HELPER" >&2
    echo "   set $var by hand from @BotFather, or install the ~/.ai skills scripts" >&2
    return 2
  }
  "$HELPER" --var "$var" --bot "$bot" --title "${title:-$t}" "$@"
}

rc=0
if [[ "$TARGET" == "--all" ]]; then
  for t in $(names); do
    ensure_one "$t" "$@" || rc=$?
  done
else
  ensure_one "$TARGET" "$@" || rc=$?
fi
exit $rc
