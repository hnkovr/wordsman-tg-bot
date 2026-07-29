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
