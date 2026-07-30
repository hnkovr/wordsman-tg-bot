#!/usr/bin/env bash
# doctor.sh — deterministic health chain for the wordsman tg-bot subproduct.
#
# Walks the pipeline outside-in and names the FIRST broken link, so "debug the bot"
# is one command instead of reading polling logs. Six layers produce the same
# user-visible symptom ("I couldn't find subtitles"); this says which one failed.
#
# Chain: config/.env → wordsman root → subtitle provider (DNS + live search)
#        → API health (if serving) → end-to-end sample (--e2e, opt-in, slow).
#
# Usage: scripts/doctor.sh [--e2e] [--api-url URL]
# Exit codes: 0 all green | 1 a link is broken | 2 bad usage
# Read-only: never mutates config, never starts long-lived processes.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

e2e=0
api_url="${TG_BOT_API_URL:-http://localhost:8340}"
while [ $# -gt 0 ]; do
  case "$1" in
  --e2e)
    e2e=1
    shift
    ;;
  --api-url)
    api_url="$2"
    shift 2
    ;;
  -h | --help)
    sed -n '2,14p' "$0"
    exit 0
    ;;
  *)
    echo "doctor: unknown argument: $1" >&2
    exit 2
    ;;
  esac
done

fail=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad() {
  printf '  \033[31m✗\033[0m %s\n' "$1"
  fail=1
}

# --- Layer 1: secrets & config -------------------------------------------------
echo "1. config"
token=""
for env_file in config/.env .env; do
  [ -z "$token" ] && [ -f "$env_file" ] &&
    token="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_file" | tail -1)"
done
[ -z "$token" ] && token="${TELEGRAM_BOT_TOKEN:-}"
if [ -n "$token" ]; then
  pass "TELEGRAM_BOT_TOKEN present"
else
  bad "TELEGRAM_BOT_TOKEN missing (config/.env, .env, or env) — bot cannot start"
fi

providers="${TG_BOT_SRT_PROVIDERS:-}"
for env_file in config/.env .env; do
  [ -z "$providers" ] && [ -f "$env_file" ] &&
    providers="$(sed -n 's/^TG_BOT_SRT_PROVIDERS=//p' "$env_file" | tail -1)"
done
# Fall back to config.yml — the real source of the default. Hardcoding a guess here made
# the doctor report `yify,podnapisi` while config.yml already said something else.
if [ -z "$providers" ] && [ -f config/config.yml ]; then
  providers="$(sed -n '/^srt_providers:/,/^[^[:space:]-]/p' config/config.yml |
    sed -n 's/^[[:space:]]*-[[:space:]]*//p' | paste -sd, -)"
fi
if [ -n "$providers" ]; then
  pass "srt providers: $providers"
else
  bad "no provider list resolved from env or config/config.yml"
fi

# --- Layer 2: wordsman root ----------------------------------------------------
echo "2. wordsman root"
root="${TG_BOT_WORDSMAN_ROOT:-}"
[ -z "$root" ] && root="$(cd ../.. && pwd)" # submodule layout
if [ -f "$root/main.py" ] && [ -f "$root/scripts/fetch_srt.sh" ]; then
  pass "wordsman root: $root"
else
  bad "wordsman root not found (need main.py + scripts/fetch_srt.sh); set TG_BOT_WORDSMAN_ROOT"
  echo
  echo "doctor: stopping — later checks depend on the wordsman root"
  exit 1
fi

# --- Layer 3: subtitle providers (DNS + live search) ---------------------------
echo "3. subtitle providers"
first_provider="${providers%%,*}"
declare -A HOSTS=(
  [yify]=yifysubtitles.ch
  [podnapisi]=www.podnapisi.net
  [gestdown]=api.gestdown.info
  [subtitlecat]=www.subtitlecat.com
)
IFS=',' read -ra plist <<<"$providers"
reachable=""
for p in "${plist[@]}"; do
  host="${HOSTS[$p]:-}"
  if [ -z "$host" ]; then
    warn "$p: no known host to DNS-probe (skipping)"
    continue
  fi
  if host "$host" >/dev/null 2>&1 || nslookup "$host" >/dev/null 2>&1; then
    pass "$p DNS ok ($host)"
    [ -z "$reachable" ] && reachable="$p"
  else
    warn "$p DNS FAILS ($host) — dead provider, will be skipped (wordsman#22)"
  fi
done
if [ -z "$reachable" ]; then
  bad "no configured provider resolves in DNS — every fetch will 404"
else
  if [ "$reachable" != "$first_provider" ]; then
    warn "first provider '$first_provider' is unreachable; '$reachable' will carry fetches"
  fi
fi

# --- Layer 4: API health (only if already serving) -----------------------------
echo "4. API (${api_url})"
if health="$(curl -fsS --max-time 5 "$api_url/healthz" 2>/dev/null)"; then
  pass "healthz: $health"
else
  warn "not serving at $api_url (start with 'make tg-bot-serve' — not required for this check)"
fi

# --- Layer 5: chat scope & group-read permission -------------------------------
# Being scoped to a topic the bot cannot read is a silent failure: the bot looks healthy,
# answers DMs fine, and never reacts in the topic. getMe reports the privacy setting.
echo "5. chat scope"
read_cfg() {
  local key="$1" value=""
  value="$(printenv "TG_BOT_${key^^}" 2>/dev/null || true)"
  for env_file in config/.env .env; do
    [ -z "$value" ] && [ -f "$env_file" ] &&
      value="$(sed -n "s/^TG_BOT_${key^^}=//p" "$env_file" | tail -1)"
  done
  [ -z "$value" ] && [ -f config/config.yml ] &&
    value="$(sed -n "s/^${key}:[[:space:]]*//p" config/config.yml | tail -1)"
  printf '%s' "$value"
}

chat_id="$(read_cfg service_chat_id)"
thread_id="$(read_cfg service_thread_id)"
topic_only="$(read_cfg service_topic_only)"
[ -z "$topic_only" ] && topic_only=true

if [ -z "$chat_id" ]; then
  warn "no service chat configured — the bot answers private chats only"
elif [ -n "$thread_id" ] && [ "$topic_only" != false ]; then
  pass "scoped to chat $chat_id topic $thread_id (plus private chats)"
else
  warn "scoped to ALL topics of chat $chat_id — set TG_BOT_SERVICE_THREAD_ID + topic_only"
fi

if [ -n "$token" ] && [ -n "$chat_id" ]; then
  me="$(curl -fsS --max-time 8 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null || true)"
  if [ -z "$me" ]; then
    warn "getMe failed (network or bad token) — cannot verify group-read permission"
  elif printf '%s' "$me" | grep -q '"can_read_all_group_messages":true'; then
    pass "privacy mode OFF — the bot receives plain messages in the topic"
  else
    bad "privacy mode ON — Telegram delivers only commands/replies/@mentions from the group"
    echo "      fix: BotFather → /setprivacy → this bot → Disable, then re-add it to the group"
  fi
fi

# --- Layer 6: end-to-end sample (opt-in, slow) ---------------------------------
if [ "$e2e" = 1 ]; then
  echo "6. end-to-end (Inception via $providers)"
  if srt="$(bash "$root/scripts/fetch_srt.sh" --movie Inception --providers "$providers" 2>/dev/null | tail -1)" &&
    [ -f "$srt" ]; then
    pass "fetched $(basename "$srt")"
  else
    bad "end-to-end fetch failed for a known-good title — inspect layer 3"
  fi
fi

echo
if [ "$fail" = 0 ]; then
  echo "doctor: healthy"
  exit 0
fi
echo "doctor: found a broken link (see ✗ above)"
exit 1
