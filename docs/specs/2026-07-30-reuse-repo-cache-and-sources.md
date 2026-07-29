# tg-bot: reuse repo cache (data/in, data/out) + widen fetch sources + tiered logging

Status: TODO (captured pre-compaction 2026-07-30). Both repos clean+pushed at capture time.

## Why (the trigger)

A user sent the bot `odyssey` (film is *"The Odyssey" (2026)*). The bot's live fetch failed:

```
No subtitles found for 'odyssey' — ProviderError: all providers failed for 'odyssey':
  yify: yify movie page failed: HTTP 404;
  podnapisi: podnapisi search transport error: [Errno 8] nodename nor servname provided
```

Yet on disk (from an EARLIER CLI/skill run, Jul 23) already existed:

- `data/in/odyssey-2026/the-odyssey-2026-cam-rip-onlyflix-1080p-en.srt` (source: "onlyflix" cam-rip — NOT yify)
- `data/out/odyssey-2026/` (full wordlist set)

Root cause: the bot **only does a live network fetch** into its own scratch
(`subproducts/tg-bot/work/<user_id>/<slug>/srt`) via `scripts/fetch_srt.sh --providers
yify,podnapisi`. It never consults `data/in/` or `data/out/`, and it uses a narrower
provider set than the CLI/skill flow that actually obtained the file. yify 404'd on the
bare query `odyssey` (title is "The Odyssey"); podnapisi is DNS-dead (wordsman#22).

## Requirements (from user)

1. **Same sources.** The bot must search the SAME sources the CLI/skill flow used to
   populate `data/in/odyssey-2026/` — not just `yify,podnapisi`. First inspect what
   `search-download-srt-for` (parent repo skill) and `srt-search`
   (`subproducts/srt-search/config/config.yml` — `dual_subtitle_sources`, subtitlecat,
   downsub, etc.) actually used to get the "onlyflix" cam-rip; then make the bot's default
   `srt_providers` / fetch flow match that fuller set (or call the same skill path).

2. **Repo cache reuse.** Before (or instead of) a live fetch, reuse already-processed
   artifacts of the CURRENT repo:
   - if `data/in/<slug>/*.srt` exists → use it, skip the network fetch;
   - if `data/out/<slug>/` already has wordlists → serve those directly, skip generation.
   Slugs already match (`odyssey-2026`), so this would have turned the failure into an
   instant hit. **Gating:** only when running against the current wordsman repo AND its
   Git-LFS content can be cloned on the same host where the bot is deployed (LFS holds the
   `data/` subtitle/wordlist blobs). Add a config flag, e.g. `TG_BOT_USE_REPO_CACHE`
   (bool) + `TG_BOT_REPO_DATA_DIR` (default `<wordsman_root>/data`), defaulting to on only
   when the data dir + LFS objects are actually present locally; off/degrade cleanly
   otherwise (remote deploy without LFS → live fetch as today).

3. **Tiered logging.** Add detailed logging at differentiated levels across all of the
   above: DEBUG for each cache-path probe and per-source attempt; INFO for a cache hit,
   the chosen source, and reuse-vs-fetch decisions; WARNING for cache miss / provider
   fallback / LFS-unavailable degrade; ERROR for total failure. Respect
   `TG_BOT_LOG_LEVEL`; loguru is already configured in `src/tg_bot/logger.py`.

## Where to implement (files)

- `subproducts/tg-bot/src/tg_bot/pipeline.py` — `fetch_srt`, `build_wordlists`,
  `movie_to_wordlists`, `document_to_wordlists`, `_work_dir`, `slugify`. Add a
  cache-lookup step before `fetch_srt`, and a `data/out/<slug>` short-circuit before
  `build_wordlists`.
- `subproducts/tg-bot/src/tg_bot/config.py` — new settings: `use_repo_cache`,
  `repo_data_dir`, and reconsider `srt_providers` default (widen).
- `subproducts/tg-bot/config/config.yml` + `config/.env.template` — expose the new knobs.
- `scripts/fetch_srt.sh` (parent) — note: its default `--out` is `data/in/$slug`; the bot
  overrides `--out` to work-scratch. Cache reuse can read `data/in/<slug>` directly.
- Tests: `subproducts/tg-bot/tests/test_pipeline.py` (cache-hit skips fetch; data/out
  short-circuit; LFS-absent degrade), keep hermetic (fake wordsman root + fake data dirs).
- Docs: fold into `docs/TROUBLESHOOTING.md` + the parent DEVELOPER-GUIDE auto-upsert block
  (`.tmp/upsert_tg_bot.py`). Run 96+ test suite; ruff/shellcheck clean; bump submodule pin.

## Open questions to resolve during impl

- Exactly which source produced `the-odyssey-2026-cam-rip-onlyflix-1080p-en.srt` — inspect
  `search-download-srt-for` skill + `srt-search` providers/config; "onlyflix" suggests a
  browser-assisted or dual-sub source, not the keyless yify path.
- Cache trust: reuse blindly, or validate the SRT (non-empty, parses) before serving?
- Multi-user: repo cache is shared/global (not per-user); per-user prefs still apply at the
  `subtitle-dict` step, so a `data/out/<slug>` short-circuit must respect the user's
  formats/level prefs — likely only short-circuit the SRT (data/in) reuse, still run
  per-user generation, unless prefs are default.

## Session state at capture

- tg-bot repo HEAD pushed; parent `feat/productionize-green-ci` @ `365e533`, CI green.
- Multi-user SQLite prefs + settings menu + `/files` send-menu already shipped.
- Provider fix (yify first) + doctor + real-error-surfacing already shipped.
- Refs GitHub issue #29 (per-user interactive mode) and #22 (podnapisi DNS-dead).
