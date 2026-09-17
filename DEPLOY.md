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
poller is deliberately slow and boring: serial, one company and one page at a
time, ~0.5s between requests and ~2s between companies, both jittered ±30%, with
an honest `User-Agent` naming the tool and your repo.

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

A read-only page of open matching reqs, browsable from anywhere, with the laptop
switched off. **It is static files and nothing else** — no server, no API, no
serverless function, no refresh button that calls anything. A visitor cannot
trigger a poll, touch the store or reach this machine, because the deployed
artifact is inert. The only writer is this laptop pushing a commit.

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

3. **Cloudflare Pages** → Create project → Connect to Git → pick the private repo.
   - Production branch: **`site`**
   - Framework preset: **None**. Build command: **empty**. Output directory: **`/`**
   - After the first deploy: Settings → **Access Policy** → require your email.
     The repo being private does not gate the deployed site; Access does.

`site` is an orphan branch holding a single commit that is replaced every push,
so the repo does not accumulate a JSON blob per run. The working tree is never
touched — publishing builds the commit through a temporary git index, so a run
interrupted by sleep leaves nothing to clean up. `docs/` is gitignored on the
main branch for the same reason.

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
a **✓ Done** button per row that ticks a job off — it disappears from that
company's list and stays gone across reloads, with a "Show ticked" toggle to get
it back — and a **copy-req-id button per row**, since retyping a requisition ID into a referral
request from a phone is the actual friction. Rows first seen since your last
visit are badged NEW (`localStorage`, wrapped in try/catch, renders fine when
empty or blocked). `generated_at` shows as "updated 3h ago", and past 24h the
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
