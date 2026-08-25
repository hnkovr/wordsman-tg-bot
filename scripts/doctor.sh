#!/usr/bin/env bash
# doctor.sh — deterministic health chain for the wordsman tg-bot subproduct.
#
# Walks the pipeline outside-in and names the FIRST broken link, so "debug the bot"
# is one command instead of reading polling logs. Six layers produce the same
# user-visible symptom ("I couldn't find subtitles"); this says which one failed.
#
# Chain: config/.env → wordsman root → subtitle provider (DNS + live search)
#        → API health (if serving) → Telegram transport (webhook vs polling)
#        → chat scope → end-to-end sample (--e2e, opt-in, slow).
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

# Trailing ` # …` comments are legal in both .env and config.yml, and the readers below
# are line-greps, so without this a commented key yields the COMMENT as its value: the
# token turns into a malformed URL and every Telegram call fails for no visible reason.
# Mirrors python-dotenv: a `#` only starts a comment when whitespace precedes it.
clean_val() {
  printf '%s' "$1" | sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' \
    -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Resolve a TG_BOT_* setting the same way pydantic-settings does: env, then env
# files, then config.yml. Used by several layers below.
read_cfg() {
  local key="$1" value=""
  value="$(printenv "TG_BOT_${key^^}" 2>/dev/null || true)"
  for env_file in config/.env .env; do
    [ -z "$value" ] && [ -f "$env_file" ] &&
      value="$(sed -n "s/^TG_BOT_${key^^}=//p" "$env_file" | tail -1)"
  done
  [ -z "$value" ] && [ -f config/config.yml ] &&
    value="$(sed -n "s/^${key}:[[:space:]]*//p" config/config.yml | tail -1)"
  clean_val "$value"
}

# --- Layer 1: secrets & config -------------------------------------------------
echo "1. config"
token=""
for env_file in config/.env .env; do
  [ -z "$token" ] && [ -f "$env_file" ] &&
    token="$(clean_val "$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_file" | tail -1)")"
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

# --- Layer 2b: RU search (optional — /ru, /ru_subs, /ru_audio) -----------------
# Non-fatal: the RU legs degrade gracefully in the bot; this only explains which
# legs will come back empty. Prints resolved values on purpose — get_settings()
# is lru_cached, so a running bot keeps OLD values until restarted.
echo "2b. RU search (optional)"
search_root="$(read_cfg search_wordsman_root)"
if [ -z "$search_root" ]; then
  warn "search_wordsman_root unset — the 'already on disk' leg is disabled"
else
  search_root_expanded="${search_root/#\~/$HOME}"
  if [ -f "$search_root_expanded/main.py" ] && [ -d "$search_root_expanded/wordsman/search" ]; then
    pass "search root: $search_root_expanded (wordsman.search present)"
  else
    warn "search_wordsman_root=$search_root lacks main.py or wordsman/search — local scan empty"
  fi
fi
if [ -f "$root/subproducts/srt-search/pyproject.toml" ]; then
  pass "srt-search subproduct present (online RU subs leg)"
  # The result buttons download through `srt-search fetch`. An older pin still SEARCHES
  # fine, so the failure would only show up as every download button erroring in chat.
  if grep -q '^def fetch(' "$root/subproducts/srt-search/src/srt_search/cli.py" 2>/dev/null; then
    pass "srt-search has 'fetch' (result buttons can deliver .srt files)"
  else
    warn "srt-search pin predates 'fetch' — search works, download buttons will fail"
  fi
else
  warn "no subproducts/srt-search under $root — online RU subs leg disabled"
fi
if [ -f "$root/subproducts/audio-search/pyproject.toml" ]; then
  pass "audio-search subproduct present (online RU audio leg)"
else
  warn "no subproducts/audio-search under $root — expected on main checkouts (feat-branch only)"
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

# --- Layer 4b: Telegram transport (webhook vs polling) -------------------------
# Which deployment Telegram actually talks to is invisible from this machine otherwise:
# a stale webhook makes a local `tg-bot bot` refuse to start, and a local webhook left
# registered by mistake silently swallows every update the cloud bot should answer.
echo "4b. Telegram transport"
public_url="$(read_cfg public_url | tr -d '"')" # config.yml keeps it quoted-empty
if [ -z "$token" ]; then
  warn "no token — cannot ask Telegram which transport owns this bot"
else
  info="$(curl -fsS --max-time 8 "https://api.telegram.org/bot${token}/getWebhookInfo" 2>/dev/null || true)"
  hook_url="$(printf '%s' "$info" | sed -n 's/.*"url":"\([^"]*\)".*/\1/p')"
  pending="$(printf '%s' "$info" | sed -n 's/.*"pending_update_count":\([0-9]*\).*/\1/p')"
  hook_err="$(printf '%s' "$info" | sed -n 's/.*"last_error_message":"\([^"]*\)".*/\1/p')"
  if [ -z "$info" ]; then
    warn "getWebhookInfo failed (network or bad token)"
  elif [ -z "$hook_url" ]; then
    pass "no webhook registered — long polling ('tg-bot bot') owns this token"
  else
    pass "webhook: $hook_url (pending=${pending:-0})"
    [ -n "$hook_err" ] && warn "Telegram's last delivery failed: $hook_err"
    if [ -n "$public_url" ]; then
      case "$hook_url" in
        "${public_url%/}"/*) ;;
        *) warn "TG_BOT_PUBLIC_URL here ($public_url) is NOT what Telegram delivers to" ;;
      esac
    fi
    [ -n "${pending:-}" ] && [ "${pending:-0}" -gt 20 ] &&
      warn "$pending updates queued — the receiving deployment is not answering"
  fi
fi

# --- Layer 5: chat scope & group-read permission -------------------------------
# Being scoped to a topic the bot cannot read is a silent failure: the bot looks healthy,
# answers DMs fine, and never reacts in the topic. getMe reports the privacy setting.
echo "5. chat scope"
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
