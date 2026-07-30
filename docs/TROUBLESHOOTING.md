# Troubleshooting the wordsman tg-bot

The bot, API, polling and pipeline can all be healthy while every request still returns
*"I couldn't find subtitles"* — six distinct layers produce that one symptom. Run the
doctor first; it walks the chain outside-in and names the **first** broken link.

```bash
make doctor            # config → wordsman root → provider DNS → API health
just doctor --e2e      # + a live sample fetch (slow, hits the network)
```

## The chain

| Layer | Green means | Typical failure |
| --- | --- | --- |
| 1. config | `TELEGRAM_BOT_TOKEN` present; provider list resolved | token only in the *other* checkout's `config/.env`; bot can't start |
| 2. wordsman root | `main.py` + `scripts/fetch_srt.sh` found | standalone checkout without `TG_BOT_WORDSMAN_ROOT` |
| 3. providers | at least one provider resolves in DNS | **podnapisi is DNS-dead** (`Errno 8`, wordsman#22) |
| 4. API | `/healthz` answers | service not started (`make tg-bot-serve`) |
| 5. e2e | a known-good title fetches an SRT | see layer 3 |

## Repo cache (data/in, data/out)

Before any network call the bot reuses the wordsman checkout's own artifacts:

| Cache | Path | Effect |
| --- | --- | --- |
| Subtitles | `data/in/<slug>/*.srt` | skips the network fetch entirely |
| Wordlists | `data/out/<slug>/` | served verbatim — no generation — but **only** when the user's level/top/formats are the global defaults; customised prefs regenerate |

Those blobs live in **Git-LFS**, so a deploy host that never pulled the LFS objects has no
cache: unfetched pointers and an absent `data/` both log a WARNING and degrade to a live
fetch. Knobs: `TG_BOT_USE_REPO_CACHE` (default true), `TG_BOT_REPO_DATA_DIR` (default
`<wordsman_root>/data`). Set `TG_BOT_LOG_LEVEL=DEBUG` to see every probe and decision —
DEBUG per path probed, INFO on hits and the chosen path, WARNING on degrades.

## Chat scope: one topic, and being able to read it

Two independent things, and confusing them costs an evening:

| Concern | Decided by | Symptom when wrong |
| --- | --- | --- |
| **Where the bot answers** | this repo (`scope.py`) | bot replies in every topic of the group |
| **Whether it receives group messages at all** | BotFather privacy mode | bot is silent in the topic; DMs work fine |

**Scope** is `TG_BOT_SERVICE_CHAT_ID` + `TG_BOT_SERVICE_THREAD_ID` with
`TG_BOT_SERVICE_TOPIC_ONLY=true` (the default). `ScopeMiddleware` is an *outer* middleware,
so an out-of-scope update is dropped before any handler, filter or service-chat
notification runs. Private chats always pass — end users still DM the bot. The General
topic never matches a configured thread, which is the intent: one topic, not the group.
Replies need no work: aiogram ≥3 fills `message_thread_id` from the incoming message.

**Reading** is a Telegram-side setting this repo cannot change. With privacy mode ON (the
BotFather default) the bot receives only commands, replies to itself and @mentions — plain
text in the topic never arrives, and nothing errors. `getMe().can_read_all_group_messages`
reports it, so `make doctor` layer 5 says so outright:

```
5. chat scope
  ✓ scoped to chat -100… topic 800 (plus private chats)
  ✗ privacy mode ON — Telegram delivers only commands/replies/@mentions from the group
      fix: BotFather → /setprivacy → this bot → Disable, then re-add it to the group
```

The re-add matters: the privacy change does not apply to groups the bot is already in.

## Proving the chain end-to-end (integration tests)

`tests/test_integration_odyssey.py` runs the whole chain against the **real** wordsman
checkout for *"The Odyssey" (2026)* — the query that first exposed all of the above. It is
deselected from the default run (`-m "not integration"` in `pyproject.toml`) because it
needs the LFS blobs and, in one lane, the network:

```bash
just test-integration          # repo cache + real subtitle-dict, offline
just test-integration-network  # + a live provider fetch
```

| Lane | Asserts | Reproduces |
| --- | --- | --- |
| query mapping | `odyssey 2026` → `("odyssey", 2026)` → slug `odyssey-2026` | the slug both data dirs use |
| subtitle cache | the cached SRT is real content, and non-default prefs regenerate from it | `data/in/odyssey-2026/**` |
| wordlist cache | the ZIP is byte-identical to the prebuilt tree | `data/out/odyssey-2026/**` |
| live fetch | providers still serve the title with the cache off | a fresh `data/in`-shaped SRT |

A skip rather than a failure means the fixtures could not find their inputs — the skip
message names the missing path and tells you to `git lfs pull` in the wordsman checkout.
Set `TG_BOT_WORDSMAN_ROOT` when running from a standalone clone with no sibling parent.

## The provider trap (most common)

srt-search's **own default provider list is `podnapisi` only** (`config.py:31`), and its
host `www.podnapisi.net` no longer resolves. A bot relying on that default 404s on every
title while yify — keyless and healthy — sits unused.

The fix lives in this repo, not the library: `TG_BOT_SRT_PROVIDERS` (config
`srt_providers`, default **`gestdown,yify,subtitlecat`** — the same set the CLI/skill flow
uses, per `english-apps/apps_pipeline.yml`) is forwarded to `fetch_srt.sh --providers`, so
the healthy providers are tried regardless of srt-search's default. gestdown covers TV
episodes; yify and subtitlecat cover films — subtitlecat in particular is what fetched
`data/in/odyssey-2026`, and a narrower `yify,podnapisi` set fails on titles it can serve.
Symptom in the polling log:

```
ProviderError: all providers failed for 'Inception':
  podnapisi: podnapisi search transport error: [Errno 8] nodename nor servname provided
```

"all providers failed" listing only `podnapisi` is the tell that podnapisi was the *only*
provider tried. Confirm the fix with `just doctor --e2e` (should fetch `Inception.2010…`).

The deeper fix — reordering srt-search's default so yify leads — is tracked upstream as
wordsman#22 and belongs in the `wordsman-srt-search` repo, not here.
