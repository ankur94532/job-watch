# Job Watcher

Polls public ATS job boards every 15 minutes and sends a Telegram alert the
moment a matching requisition appears, with the req ID to attach to a referral
request. Public JSON endpoints only — no headless browser, no HTML scraping, no
LinkedIn, no paid APIs.

Standard library only. It runs on a stock macOS `/usr/bin/python3`.

```sh
make test                         # 145 tests, no network
python3 -m watcher seed           # FIRST: record every open req, alert nothing
python3 -m watcher run            # fetch, diff, alert on what's new
```

Then wire it to launchd — see [DEPLOY.md](DEPLOY.md).

`make test` covers the poller. The phone view is plain DOM with no build step,
so its checks drive a real browser against a real export and live separately,
outside the stdlib-only suite:

```sh
make export
.venv-dev/bin/python devtools/site_check.py   # dev-only, like `watcher trace`
```

## Commands

| | |
| --- | --- |
| `watcher seed` | Record every board as already-seen. Run this first: `run` on an empty database would alert on thousands of open reqs. |
| `watcher run` | One sweep of whatever is due. Alerts only on requisitions never seen before. |
| `watcher status` | Per-company poll state: interval, last poll, next due, backoff, sweeps in progress. |
| `watcher list --company X --since 7d --matches` | What's in the store. |
| `watcher list --closed` | Reqs that vanished from their board, with how long they stayed open. |
| `watcher test-adapter X` | One fetch, counts and sample rows, writes nothing. Use before adding a company. |
| `watcher detect <name\|domain\|job URL>` | Work out a company's ATS and print a config block; `--write` appends it. |
| `watcher export --out docs/ [--publish]` | Render open matching reqs to a static page for the phone. No server, no API. |

Paths come from `$JOBSWATCH_CONFIG` (default `./config.yaml`) and `$JOBSWATCH_DB`
(default `~/.local/share/jobswatch/jobs.db`), overridable with `--config` / `--db`.

## Adding a company

```sh
python3 -m watcher detect qdrant.tech
python3 -m watcher detect https://acme.wd3.myworkdayjobs.com/External/job/…   # Workday needs a real job URL
python3 -m watcher detect rubrik.com --write
```

Detection fetches the careers page, follows redirects, greps the served HTML and
inline JSON for board hostnames, then probes the keyless APIs with slug guesses,
and verifies whatever it finds with one live fetch. Slug guesses are flagged:
`--write` refuses them without `--accept-guess`, because slugs are first-come
(Lever's `eternal` is a US sports company, not Zomato; SmartRecruiters answers
`200` with an empty list for *any* token). Freshness is checked too — a board
whose newest posting is a year old is reported as stale.

A new company is recorded silently on its first complete sweep, so adding one
mid-flight never floods you.

## How it decides what's new

Diffing is against stored requisition ids, never posted dates. A run 11 hours
after the last one reports every req that appeared in between, exactly once, and
a reposted-but-already-seen req stays quiet.

`(company, job_id)` is the primary key — req IDs collide across ATS platforms.
`job_id` is the board's per-posting id; `req_id` is the requisition number to
quote in a referral. They differ more often than you'd think: one req posted to
three locations is three postings sharing one req ID, and keying on the req ID
alone would silently drop the Bangalore one.

## Filters

`config.yaml` `defaults.title` / `defaults.location`, overridable per company.
Terms match on word boundaries after punctuation is normalised, so
`Software Engineering - MTS` matches `mts` and `Back-End` matches `back end`,
while `Systems` does not match `mts`.

There is no seniority or years-of-experience filtering, on purpose: Indian
offices post generic "Software Engineer" reqs with no level attached. Order the
title list most-wanted first — alert ranking uses that order.

## Layout

```
config.yaml              companies, filters, per-company intervals
watcher/
  adapters/              greenhouse, lever, ashby, smartrecruiters, workday,
                         eightfold, oracle, jibe, rippling, radancy, amazon, atlassian
                         + README.md: what each endpoint actually returns
  store.py               SQLite: dedup, first/last seen, closed reqs, poll state
  runner.py              one sweep: due → fetch → diff → notify
  filters.py             title/location matching and alert ranking
  notify.py              batching, digests, Telegram; any sink drops in
  detect.py              ATS detection
  http.py                honest UA, conditional GET, retry/backoff
  yamlish.py             small YAML-subset parser (keeps the runtime dependency-free)
  export.py              static render: company index + one file per company
  publish.py             direct Cloudflare upload (or legacy git-branch publishing)
  site/                  the page: companies -> company -> paginated jobs, no build step
deploy/                  launchd plist template + env example
devtools/                site_check.py: drives the page in a real browser (dev-only)
tests/                   fixtures recorded from live boards; no network
```

## Non-goals

No auto-apply, no LinkedIn, no headless browser, no paid scraping APIs, no
daemon. Companies whose board is not reachable as public keyless JSON — or that
refuse an honest User-Agent — are recorded as `ats: blocked` with a typed reason
and skipped, rather than scraped or spoofed.

Currently **69 companies polled, 17 not**. Every one of the 17 is typed
`robots`, `dynamic-challenge`, `server-rendered` or `no-board`, and
[EVIDENCE.md](EVIDENCE.md) gives the exact request/response pair behind each
verdict. The three `robots` cases are never requested at all — only their
`robots.txt` is read, and the disallow line is quoted.
