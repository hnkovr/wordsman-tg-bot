# Deploying @wordsman_bot

The bot ships as one image (`deploy/Dockerfile.cloud`), and **every host runs it against
its own Telegram bot** — `deploy/targets.yml` is the registry. Fly is production
(@wordsman_bot), Render is staging (@wordsman_render_bot); Amvera, Railway and Hetzner
have names reserved but are not provisioned.

## One bot per host

A token has exactly one webhook URL, so two deployments sharing a token must negotiate
who owns it — and a mistake there is silent: the loser simply stops receiving updates.
Giving each host its own bot removes the negotiation entirely. Separate tokens cannot
contend, so staging can be redeployed, broken and re-pointed at any time without
production noticing.

The ownership rules below still apply *within* one bot — a local `tg-bot serve` and the
cloud deployment of the same target do share a token — but they are no longer what keeps
the fleet apart.

```bash
just targets                      # the fleet: role, bot, token variable
just bot-token render             # ensure that target's token (asks only if it must)
just bot-tokens-check             # verify every token, never prompt
```

`scripts/bot-token.sh` resolves a token from **every** store on the machine — process
env, `~/.ai/.env.secrets`, project env files, the macOS keychain, Bitwarden — before
asking anyone. If it must ask, it first probes `https://t.me/<handle>` to tell "fetch the
existing bot's token" from "create the bot", opens @BotFather in the Telegram app, and
collects the value in a macOS dialog carrying those exact steps. Whatever comes back is
checked against `getMe` immediately: a token that belongs to a *different* bot is
rejected outright rather than persisted, because that failure (a deployment running
@AhmadRuIT_bot) has already cost this project a debugging session.

## How updates reach the bot

## Two transports, one token

| | Long polling (`tg-bot bot`) | Webhook (`tg-bot serve`) |
| --- | --- | --- |
| Who initiates | the bot calls `getUpdates` in a loop | Telegram POSTs to a public HTTPS URL |
| Needs a process that never sleeps | **yes** | no |
| Works on a free/trial host that stops when idle | no | **yes** — the delivery wakes it |
| Conflict mode | a second consumer gets HTTP 409 | the last `setWebhook` silently wins |
| Used for | local development | both cloud deployments |

Telegram allows exactly one of the two per token: registering a webhook makes
`getUpdates` fail. `run_bot()` checks `getWebhookInfo` up front and exits with the URL
that owns the token, rather than dying on a 409 later.

Two rules decide who owns the bot:

1. A deployment is *eligible* only when `TG_BOT_PUBLIC_URL` is set. Unset (a local
   `tg-bot serve`) means it never talks to Telegram at all.
2. An eligible deployment claims the webhook on startup **only when the webhook is unset
   or already its own** — never from a live owner. Taking over is deliberate:
   `tg-bot webhook set` on the new host (or `TG_BOT_WEBHOOK_FORCE_CLAIM=true`).

Rule 2 exists because rule 1 alone is not enough. With unconditional registration, a
demoted host re-claimed the bot the moment *anything* woke it — a health check was enough
to undo a failover silently (observed 2026-08-20 during a Fly→Render drill). `/healthz`
now distinguishes the two: `telegram: "webhook"` = this deployment owns the bot,
`"standby"` = eligible but someone else owns it, `"off"` = not configured for it.

That field reports what the process decided **at its own startup**, so a long-running
host that was demoted afterwards keeps saying `webhook` until it restarts. It is a
cheap liveness hint, not the authority — `tg-bot webhook info` and `scripts/doctor.sh`
layer 4b ask Telegram, which is.

## Failover between hosts

Both hosts can be eligible at once; only one owns the bot. To move it:

```bash
# on the host you are promoting (Render env vars / fly secrets):
#   TG_BOT_PUBLIC_URL=<its own https URL>   TG_BOT_WEBHOOK_SECRET=<same secret>
tg-bot webhook set          # deliberate takeover, or hit /tg/webhook after a redeploy
tg-bot webhook info         # confirm the URL flipped
```

The old owner needs no shutdown: it will notice at its next start that it no longer owns
the bot, log a warning and serve as a standby. Measured 2026-08-20 — Fly wakes a stopped
machine in ~9–10 s, Render answers a warm request in 0.24 s but sleeps after ~15 min idle
with a ~50 s cold start. Fly stays primary for that reason; Render is a warm spare.

## Why webhook mode is the default here

Fly's free trial stops a machine five minutes after it wakes:

```
Trial machine stopping. To run for longer than 5m0s, add a credit card by visiting https://fly.io/trial.
```

A poller cannot survive that. A webhook does not care: the machine is stopped between
updates, Telegram's POST boots it (`auto_start_machines = true`), and the bot answers
from a cold start in a couple of seconds. Render's free plan sleeps on idle for the same
reason and would work the same way, just with a ~50 s cold start.

Adding a credit card is therefore optional now. It is still worth doing for one reason —
see the caveat below.

## Deploy

```bash
just deploy-fly            # flyctl deploy --ha=false
just fly-status
just fly-logs
just render-redeploy       # Render also autodeploys from main
```

`--ha=false` is mandatory: a second machine would answer deliveries with its own copy of
the bot.

Secrets on Fly (never printed, never committed):

```bash
printf 'TELEGRAM_BOT_TOKEN=%s' "$WORDSMAN_TG_BOT_TOKEN" | fly secrets import -a wordsman-tg-bot
fly secrets set TG_BOT_WEBHOOK_SECRET="$(openssl rand -hex 16)" -a wordsman-tg-bot
```

`TG_BOT_PUBLIC_URL` is plain config, not a secret — it lives in `fly.toml [env]`.

> **Token trap.** `~/.ai/.env.secrets:TG_BOT_TOKEN` is **@AhmadRuIT_bot**, a different
> bot. @wordsman_bot's token is `WORDSMAN_TG_BOT_TOKEN`. Verify with `getMe` *before*
> deploying — a wrong token deploys a bot that answers in someone else's chats.

## Verify a deployment

```bash
# 1. the service is up and knows it owns the Telegram side
curl -s https://wordsman-tg-bot.fly.dev/healthz     # {"telegram":"webhook",...}

# 2. Telegram agrees, and has nothing stuck in the queue
tg-bot webhook info    # url=…/tg/webhook pending=0 last_error=(none)

# 3. the endpoint refuses forged updates
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://wordsman-tg-bot.fly.dev/tg/webhook -H 'content-type: application/json' -d '{}'
# → 403

# 4. end-to-end: a real /help in Telegram, or a signed synthetic update
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://wordsman-tg-bot.fly.dev/tg/webhook \
  -H "X-Telegram-Bot-Api-Secret-Token: $TG_BOT_WEBHOOK_SECRET" \
  -H 'content-type: application/json' -d @update.json
# → 200, and the reply lands in the chat named by update.json
```

Step 4 is the only one that proves the handlers run. `healthz` alone has never caught a
broken deployment here.

Running arbitrary code inside the deployed container (non-obvious — `fly ssh console -C`
splits on spaces and strips plain quotes, so the payload must be space-free with escaped
quotes):

```bash
fly ssh console -a wordsman-tg-bot -C "python -c exec(__import__('base64').b64decode('<B64>'))"
```

## Caveats

- **Trial ceiling.** Handlers run in the background after the 200, and a search can take
  up to `ru_search_timeout` (120 s). Within the trial's five-minute window that is fine,
  but an update arriving near the end of the window can be cut off mid-search. Adding a
  card removes the window entirely — that is now the only reason to add one.
- **Ephemeral state.** `data/tg_bot.sqlite3` (per-user prefs) resets on every deploy on
  both platforms. Attach a Fly volume if prefs must survive.
- **No local media in the cloud.** `TG_BOT_SEARCH_WORDSMAN_ROOT` is deliberately unset in
  the image, so the 📀 "already on disk" leg of `/ru*` is disabled there by design. The
  online legs and the links-only sources report work normally.
- **Privacy mode.** Plain (non-command) text in a group topic reaches the bot only with
  BotFather's privacy mode disabled; `scripts/doctor.sh` reports it.
