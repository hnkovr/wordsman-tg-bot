# Handoff — RU search result buttons + the build blocker (2026-08-26)

Open issues: **#49** (build blocker — nothing deploys until this lands), **#51** (rotate
the leaked webhook secret, rides #49's deploy), **#46** (feature; needs one human check).

Tracker: hnkovr/wordsman [#46](https://github.com/hnkovr/wordsman/issues/46) (feature),
[#49](https://github.com/hnkovr/wordsman/issues/49) (build blocker + publication incident).

## Shipped and merged

`/ru*` no longer answers with a wall of text. Every result is a button; tapping one
**delivers the file** — a local hit from disk, an online candidate downloaded first
(`srt-search fetch`, `SRT_SEARCH_LANGUAGE=ru`) and attached as `.srt`. `⬇️ Скачать все`
sends every online subtitle. Audio tracks and torrent/manual sources stay link buttons:
a track is a whole media file, far past Telegram's 50 MB ceiling.

- `wordsman-tg-bot` main `41faef3` — `picks.py` (result cache), `rusearch.render_results`
  + `collect_picks` + `download_online_sub`, `bot._send_picks`/`_deliver_pick`.
  257 tests pass, coverage 93.5% (gate 85%).
- `wordsman-srt-search` main `0205114` — new `fetch PROVIDER CANDIDATE_ID`; subtitlecat
  now **verifies the language exists before listing a candidate** and parses real download
  counts. Probed live: of four "Interstellar" entries only one had a RU track, and it was
  not the top hit — without this, most buttons would have failed.
- `wordsman` main `6a4d7e7` — submodule pins.

Live: `@wordsman_render_bot` runs the new image (built during the brief public window).
**Not yet confirmed end-to-end by a human** — the synthetic update targets the service
topic, which staging's scope rules drop. A real `/ru_subs Interstellar` is the check.

## Blocked: nothing can be rebuilt or failed over

`deploy/Dockerfile.cloud` clones the **private** `hnkovr/wordsman`; the build has no
credentials → `could not read Username`. Consequences:

- new code cannot reach any host;
- **failover is impossible too**: Render applies env only on a deploy — restart returns
  200 with the old env, rollback returns 400. See `docs/deploy.md` § "Failover to Render
  is gated on a working build".
- `@wordsman_bot` (Fly) is down — trial ended, `flyctl` refuses even `status`; its webhook
  is unset with ~6 updates queued. No host can serve it today.

Fix options ranked in #49; the strongest is **build in GitHub Actions → push to GHCR →
Render deploys the prebuilt image** (private repo is natively reachable inside Actions, so
no token is created, stored or mounted anywhere). `scripts/render-env.py --token-var
WORDSMAN_TG_BOT_TOKEN` is the failover one-liner once builds work.

## MUST DO on the next successful deploy — rotate TG_BOT_WEBHOOK_SECRET

Its value was printed into a session transcript on 2026-08-26 (a raw `GET /env-vars` dump).
It is the only auth on `/tg/webhook`, so anyone holding that transcript can POST forged
updates to the bot; it does NOT expose the bot token. It cannot be rotated on its own,
because Telegram and the container must change together and the container's env only
changes on a deploy. So, in the SAME deploy that fixes the build:

```bash
NEW=$(openssl rand -hex 16)     # store in ~/.ai/.env.secrets as TG_BOT_WEBHOOK_SECRET
python3 scripts/render-env.py --target render        # pushes env + deploys
tg-bot webhook set                                   # re-register with the new secret
```

## Build fix — in place, waiting only on a token

`deploy/Dockerfile.cloud` now authenticates its clone with a Render **secret file** named
`gh_token`, bind-mounted for exactly one RUN (`--mount=type=bind`), so nothing lands in an
image layer — a build ARG would persist in image metadata. `gh_token` is gitignored.

Remaining step: a fine-grained, read-only (Contents:Read) GitHub token in
`GH_HNKOVR_READ_TOKEN` — the ACCOUNT-WIDE default for cloning private hnkovr repos in
builds, registered in `~/.ai/skills/_settings/secrets_catalog.yml`. Its scope must cover
every repo that should be cloneable; a project needing something narrower shadows it with
`<PROJECT>_REPO_READ_TOKEN`, which consumers try first. Then:

```bash
python3 scripts/render-env.py --target render \
    --secret-file gh_token=WORDSMAN_REPO_READ_TOKEN,GH_HNKOVR_READ_TOKEN
python3 scripts/render-env.py --target render          # build + deploy
# then the failover the owner asked for:
python3 scripts/render-env.py --target render --token-var WORDSMAN_TG_BOT_TOKEN
```

Local builds need the file in the context: `printf '%s' "$GH_HNKOVR_READ_TOKEN" > gh_token`.
Do NOT use `GH_TOKEN` (the broad gh-CLI user token) for this — it can push and delete.

## Do not repeat

`hnkovr/wordsman` was public for ~6.5 min (22:13:50–22:20:30 UTC) to unblock that build,
then reverted. 0 forks, no referrers; **re-run the traffic check** — GitHub aggregates per
day and lags ~24h:
`just -f ~/.ai/skills/_scripts/git/github-cli/Justfile exposure-report hnkovr/wordsman`

Publishing is never the unblock here: `data/` carries complete third-party subtitle
scripts. Guardrails added and pushed (`~/.ai` `80b253f`): a content audit
(`content-audit.sh`, classes in `_settings/git/publish_content_audit.yml`), an
`exposure-report.sh`, both wired into `/github-repo-set-visibility`, and a PreToolUse hook
`publish-guard.py` that denies a raw gh visibility flip. The hook registration lives in
`~/.claude/settings.json`, which is **gitignored** — it must be re-added on another machine.

## Agentic artifacts from this session (outside these repos)

- `~/.ai` — content audit + exposure report wired into `/github-repo-set-visibility`;
  `publish-guard.py` PreToolUse hook with **22 pinned cases**
  (`_scripts/git/repos/hooks/tests/test_publish_guard.py`); `GH_HNKOVR_READ_TOKEN`
  registered in `_settings/secrets_catalog.yml`; live deploy state in `_settings/wordsman.yml`.
- Memory (`~/.claude/projects/.../memory/`, **gitignored — on disk only**):
  `tg-bot-webhook-deployment` corrected to say Fly is down and Render cannot take over;
  `repo-publish-guard` added; `wordsman-repo-state` says the repo must stay private.
- `~/.claude/settings.json` registers the hook and is **gitignored** — re-add on another machine.
- `.tmp/SESSION-20260826-ru-buttons.md` (gitignored) records which scratch artifacts were
  dropped and why. Nothing there is promotion material.

## Next action, in order

1. Mint `GH_HNKOVR_READ_TOKEN` (fine-grained, Contents:Read, covering the private hnkovr
   repos builds need) → `ask-secret-gui.sh GH_HNKOVR_READ_TOKEN https://github.com/settings/personal-access-tokens/new`
2. `--secret-file gh_token=WORDSMAN_REPO_READ_TOKEN,GH_HNKOVR_READ_TOKEN` → deploy.
   This is the first real test of whether Render puts secret files in the build context
   and honours `--mount=type=bind`; if not, fall back to Actions → GHCR.
3. Same deploy: rotate `TG_BOT_WEBHOOK_SECRET` (#51), then `--token-var WORDSMAN_TG_BOT_TOKEN`
   for the failover the owner asked for.
4. Re-run `exposure-report hnkovr/wordsman` — GitHub's per-day traffic lags ~24h.
5. Human check: `/ru_subs Interstellar` to @wordsman_render_bot; a button must return a .srt.
