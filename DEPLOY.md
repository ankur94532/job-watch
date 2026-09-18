# Deploying on a Mac with launchd

One process per poll: launchd starts `watcher run`, it sweeps whatever is due,
alerts, and exits. Nothing stays resident, and nothing schedules itself.

Why launchd and not cron: the laptop sleeps with the lid closed and cron simply
skips whatever it missed. launchd with `StartInterval` runs the job as soon as
the machine wakes, so an overnight gap becomes one catch-up poll in the morning.
The diff is against stored requisition ids, never against posted dates, so that
catch-up run reports every req that appeared while the lid was shut, exactly
once.

## Where the repo lives

**Not in `~/Downloads`, `~/Desktop` or `~/Documents`.** Those are TCC-protected:
a launchd agent can `chdir` into them but every read returns `Operation not
permitted`, so `python3 -m watcher` fails with a bare `No module named watcher`
and nothing in the log explains why. This repo lives at `~/job-watch` for that
reason. The alternative — granting Full Disk Access to `/usr/bin/python3` — hands
that privilege to every script you ever run under it.

The rendered plist also sets `PYTHONPATH` to the repo, so module resolution does
not depend on launchd honouring `WorkingDirectory`.

## Install

```sh
cd ~/job-watch
make test                      # no network, ~0.2s

cp deploy/jobswatch.env.example deploy/jobswatch.env
$EDITOR deploy/jobswatch.env   # Telegram token + chat id, contact URL
```

`deploy/jobswatch.env` is gitignored. `make install` renders it into the plist's
`EnvironmentVariables` at `~/Library/LaunchAgents/com.shashwattripathi.jobswatch.plist`
with mode 600. The token is never committed.

**Seed first. This is not optional:**

```sh
make seed                      # a few minutes: every board, recorded, zero alerts
```

Seeding writes every currently-open requisition to the database as "already
seen". Skip it and the first run treats ~4,000 open reqs as new and sends you a
digest saying so — or, with a smaller config, a wall of messages. `make install`
refuses to run if the database does not exist yet.

```sh
make install                   # renders the plist, bootstraps the agent, polls once
```

`RunAtLoad` is true, so installing performs a first poll immediately.

## Verify

```sh
launchctl list | grep jobswatch
# 12345  0  com.shashwattripathi.jobswatch
#   ^pid  ^last exit status (0 = clean; 78 = plist problem)
```

A missing PID just means it is between runs — that is normal. Exit status is the
column that matters.

```sh
make status                    # what the watcher itself thinks
```

```
company                ats               every  last poll         next due          state
rubrik                 greenhouse          15m  2026-09-16 09:31  2026-09-16 09:46  ok, 138 open, etag
salesforce             workday              1h  2026-09-16 09:31  2026-09-16 10:31  ok, 1465 open
meesho                 lever               15m  2026-09-16 09:31  now               ok, 50 open, etag
```

## Force a run

```sh
make kick                      # launchctl kickstart -p, runs now, logs as usual
make run                       # or run it in this terminal, same database
```

Both are safe while the agent is loaded: a flock on `<db>.lock` means the second
one exits quietly instead of double-polling.

## Logs

```sh
make logs                      # tail -f both files
```

- `~/Library/Logs/jobswatch/jobswatch.log` — Telegram-less alerts print here.
- `~/Library/Logs/jobswatch/jobswatch.err.log` — the run summary and all logging.

A normal run:

```
09:46:02 INFO    rubrik: unchanged (304)
09:46:04 INFO    meesho: 50 open, 1 new, 1 new matches
09:46:05 INFO    run finished in 6.4s
  ok          meesho                    50 open, 1 new, 1 new matches
  unchanged   rubrik                 304
  not due     21: salesforce, adobe, …
alerts: 1 delivered
```

Lines worth reacting to:

| Line | Meaning |
| --- | --- |
| `offline — skipping run` | woke before wifi associated. Expected, nothing lost, exit 0 |
| `BACKED-OFF acme until …` | 403/429/5xx; that company is not being polled until then |
| `partial … sweep resumes next run` | big Workday board hit the 50s budget; it continues where it stopped |
| `deferred` | ran out of time this run, first in line next run |
| `FAILED` | real error — bad config or a changed API |

## Rate limiting, and what happens if a board pushes back

Everything is public and keyless, which is a courtesy, not a contract. The
poller is deliberately slow and boring towards any one server: one company and
one page at a time per host, ~0.5s between requests and ~1.2s between companies,
both jittered ±30%, with an honest `User-Agent` naming the tool and your repo.
Different hosts are polled in parallel (`parallel: 8`), because Greenhouse has
no reason to wait on Workday; every board on a shared API (all Greenhouse boards
are one host) still queues behind the others, and Workday tenants, which share
clusters, run at most three at a time. `parallel: 1` makes it fully serial again.
Multi-page sweeps stop at `run_budget` (50s) and resume next run; single-request
boards may run up to `overrun` (120s) past it, so all of them are polled every run.

Greenhouse, Lever, Ashby and SmartRecruiters all answer `304 Not Modified` to a
conditional request, so most polls transfer nothing. Workday sends
`Cache-Control: no-store` and pages 20 rows at a time, so it defaults to one
sweep an hour instead of four.

On 403 or 429 the company is parked — 30 minutes, doubling to a 24-hour ceiling,
1 hour for repeated 5xx — and the timestamp lives in SQLite, so a restarted
process does not retry it. `Retry-After` wins if it asks for longer. The first
success clears it. If a company stays parked for more than two hours you get one
Telegram message about it, once per episode, because a backed-off company looks
exactly like a company with no openings.

## Phone view (Cloudflare Pages)

A page of open matching reqs, browsable from anywhere, with the laptop switched
off. **The job data is static files** — no server behind it and no refresh
button that calls anything. A visitor cannot trigger a poll, touch the store or
reach this machine; the only writer of job data is this laptop pushing a
commit.

The one exception is **tick sync** (below): a single Pages Function,
`functions/api/state.js`, that stores what you ticked so the phone and the
laptop agree. It never sees the store, and it sits behind the same Access login.

`watcher run` renders it after any successful run:

```sh
make export                    # render into docs/ only
make site                      # render and force-push to the `site` branch
open docs/index.html           # works straight from disk, against local data
```

### One-time setup

1. **Private repo.** The list reveals what you are looking at and when you
   started; the postings themselves are public, that pattern is not.

   ```sh
   git init && git add -A && git commit -m "job watcher"
   gh repo create job-watch --private --source=. --push      # or add a remote by hand
   ```

2. **Turn publishing on** in `config.yaml`:

   ```yaml
   export:
     enabled: true
     publish: true              # was false
   ```

3. **Cloudflare Pages**: Workers & Pages → Create application → **Pages** tab
   (not Workers) → Import an existing Git repository → GitHub → the private repo.
   - Production branch: **`site`**
   - Framework preset: **None**. Build command: **empty**. Output directory: empty
     (the `/` prefix shown is the root).
   - Settings → Branch control → Preview deployments: **None**. Otherwise `main`
     (code and config) is published at an ungated preview URL.

4. **Lock it with Cloudflare Access.** The repo being private does not gate the
   deployed site, and the project's own "Preview access → Restrict previews"
   covers preview URLs only, NOT `<project>.pages.dev`. That needs Zero Trust:
   - Zero Trust (Free plan) → Access → Applications → Create new application →
     **Self-hosted and private**.
   - Public hostname → *Switch to custom input* → `<project>.pages.dev` (the
     domain dropdown lists only zones you own, and pages.dev is not one).
   - Create new policy: Action **Allow**, Include **Emails** = your address.
     Not "All authenticated users": that admits anyone with any email.
   - Leave browser rendering (RDP/SSH/VNC) off. Create.
   - Verify from a logged-out client: `curl -sI https://<project>.pages.dev/data/jobs.json`
     must answer **302** to `…cloudflareaccess.com`, not 200.

   Live since 2026-09-18 at `job-watch-7om.pages.dev`.

`site` is an orphan branch holding a single commit that is replaced every push,
so the repo does not accumulate a JSON blob per run. The working tree is never
touched — publishing builds the commit through a temporary git index, so a run
interrupted by sleep leaves nothing to clean up. `docs/` is gitignored on the
main branch for the same reason.

### Tick sync (one-time setup)

Ticks, "opened" marks and which poll each company was last opened at are kept
per device in `localStorage`, and synced through `/api/state`:

1. Workers & Pages → **KV** → Create namespace, e.g. `jobwatch-state`.
2. The Pages project → Settings → **Bindings** → Add → KV namespace. Variable
   name **`STATE`** (exactly), namespace `jobwatch-state`. Save.
3. Bindings reach new deployments only: redeploy (Deployments → latest →
   Retry deployment), or wait for the next run that changes anything.

The function verifies the Access token itself (signature against the team's
certs, issuer, expiry) and keys the document by the verified email, so a
misconfigured Access application fails closed instead of opening a write
endpoint. Merging is order-free — timestamps take the max, and a tick is
last-writer-wins with unticks kept as timestamped tombstones, so an untick on
the phone is not undone by the laptop's older tick. The page stays usable
offline and says `synced`, `not synced yet` or `sync paused: reload to sign in`
next to the freshness line.

A local copy (`127.0.0.1`, `file://`) has no endpoint. If it holds ticks, it
shows a one-time link that carries them to `export.url` in the URL fragment
(which never leaves the browser); sign in to the online copy first, because
the Access login flow does not preserve a fragment.

Checks: `make test` runs the function's tests in Node;
`devtools/sync_check.py` drives a "laptop" and a "phone" against the real
function behind a fake Access (`devtools/pages_dev.mjs`).

### What gets published

`data/jobs.json` (a company index) plus one `data/<company>.json` per company,
and `index.html`, `app.css`, `app.js`. The home screen downloads only the index —
3.5KB for 45 companies — and opening one company fetches just that file (~11KB),
rendered 25 rows at a time. The payload holds only
open, currently-matching requisitions and only these fields: company, title,
location, url, req_id, first_seen, last_seen, ats — plus `generated_at` and the
count of companies watched. Never the config, the Telegram token, backoff state,
error text, or raw ATS payloads. Today that is 884 rows across 45 files, 376KB in total, of which a phone
downloads 3.5KB to open the app.

Pushes are skipped when the payload is byte-identical to the last one, which is
most runs. A push failure is logged and the run continues — waking with no wifi
is normal.

### The page

Mobile-first, no framework, no third-party requests at all: no CDN, no web
fonts, no analytics. While the page is open it re-checks the 3.5KB index once a
minute (and whenever you return to the tab). A poll that lands mid-read is
**parked, not applied**: a "↻ 3 new roles — tap to load" pill appears and the
list under your thumb does not move until you tap it. Tapping keeps your filter
and your place in the pagination. Newest first, filter by company or title text, and a
a row you tap through to is **highlighted on your return** ("opened 4m ago"), so
after applying you can find it again without re-reading titles; a **✓ Done**
button per row ticks a job off — it disappears from that
company's list and stays gone across reloads, with a "Show ticked" toggle to get
it back — and a **copy-req-id button per row**, since retyping a requisition ID into a referral
request from a phone is the actual friction. Rows first seen since your last
visit are badged NEW (`localStorage`, wrapped in try/catch, renders fine when
empty or blocked, and synced across devices as above). `generated_at` shows as "updated 3h ago", and past 24h the
page says so in a banner — a stale dashboard that looks live is worse than none,
and a shut laptop is the normal reason.

`<meta name="robots" content="noindex, nofollow">` is on the page regardless of
hosting, because visibility settings change.

## Uninstall

```sh
make uninstall                 # database and logs are kept
```

## Moving it to a server later

Nothing in the code knows about launchd or macOS. Set `JOBSWATCH_DB` and
`JOBSWATCH_CONFIG`, run `watcher run` from any scheduler, and it behaves
identically — due times and backoff live in the database, not in the scheduler.
