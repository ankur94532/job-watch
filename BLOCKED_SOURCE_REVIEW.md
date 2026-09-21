# Inactive-source recovery review — 2026-09-19

## Intake queue blockers — 20 September, 23:00 IST

The completed 27-name intake (25 after the user removed Google DeepMind and
`scoutit`) adds four explicit inactive records. These are the only requested
companies not enabled; all 21 other requested companies are live. Each can be
rechecked from the URLs stored in `config.yaml` without guessing an ATS.

| Company | Exact blocker | Why no fallback was used |
| --- | --- | --- |
| CoinDCX | The official opportunities page and same-host `robots.txt` both return HTTP 403 to the honest watcher client. | Indexed detail snippets do not enumerate a complete current inventory or expose a verified first-party request. |
| Joveo | `careers.joveo.com` and its ten-URL job sitemap negotiate TLS 1.3 only; the deployed Python is built with LibreSSL 2.8.3 and reports `ssl.HAS_TLSv1_3=False`. | A curl subprocess would make diagnostics pass but leave the deployed Python runtime unable to poll its own source; the readable test chatbot host does not expose the production inventory. |
| Flexiple | Its current talent page explicitly says candidates have no applications to send and no feed to check; matching begins after profile creation. | `careers.flexiple.com` is a login flow and the old jobs host is gone. LinkedIn/staffing advertisements are not a complete first-party board. |
| CGI | CGI India's first-party page links `cgi.njoyn.com` with `CountryID=IN`; it immediately returns a Radware CAPTCHA. | The separate `cgi.jobs` Recruit Rooster shell currently returns an empty search API and is not CGI India's linked inventory, so it cannot stand in for Njoyn. |

No challenge was solved, no browser/user-agent impersonation or proxy was
used, and no search-engine result became an inventory row. The four config
entries use `ats: blocked` with `recheck` URLs, keeping the limitations visible
in status output and preventing accidental polling.

## Coverage update — 20 September, 20:38 IST

The first-party constraints recorded below still stand, but they no longer mean
an employer is wholly unwatched. All eight blocked first-party entries now have
clearly labelled Instahyre secondary coverage. Gupshup and Krutrim were the last
gap: Instahyre recognizes each employer and returns an authoritative zero, so
their `-instahyre` entries were silently baselined at zero and enabled. This is
complete coverage of what those recruiters publish to Instahyre, not a claim of
complete employer inventory. See the current [handoff](SESSION_HANDOFF.md).

## Follow-up findings — 20 September, 16:50 IST

**The earlier “one automatable, nine cannot” conclusion is superseded. Two
sources now have working adapters: McKinsey and Citadel Securities. Two more,
Gupshup and Blinkit, became readable but currently publish empty inventories.**
The user approved four-hour polling for McKinsey and Citadel Securities after
their temporary live baselines passed. **Both are now enabled**, following
complete silent production baselines at 16:50–16:51 IST. There are **141 tracked,
133 active and eight inactive companies**. Normal scheduled polls under these
new adapters have not yet been observed.

| Company | Current evidence | Automation path |
| --- | --- | --- |
| McKinsey | Repeated 595/595 unique jobs, 46 with India locations, one title/location match | Implemented `mckinsey`; one counted response, four-hour interval |
| Citadel Securities | Official form POST returns 84/84 unique postings; four list Gurugram, one title match | Implemented `citadel_securities`; one counted response, four-hour interval |
| Gupshup | Company config identifies Gupshup, `recruitment_enabled=false`; correct POST returns `status=success`, `data=[]`, `job_counts=0` | Readable public feed; monitor for reopening before validating a positive-job parser |
| Blinkit | Direct 200 careers page, “0 of 0 results”; published JS filters existing HTML rows, none present | Readable empty board; positive-job parsing remains unvalidated |
| Flipkart / Ola | Both served explicit robots `Disallow: /` again | Approved employer feed or native alerts; no current direct polling route under project policy |
| Instabase | Officially linked Greenhouse API still 404 | Monitor the official careers page for a restored/new board; indexed historical rows are not live inventory |
| Zepto | Official careers still links TalentRecruit; direct tenant request still 403 | A readable official tenant/feed remains needed; rejected Zwayam aliases prove only those mappings invalid |
| Krutrim | Official AI Labs CTA still points to LinkedIn Jobs | Native job-alert emails offer a possible separate notification source; no direct watcher source validated |
| Zomato / Eternal | Previously inspected first-party careers component is referral-only | Employer/referral announcements or an employer-provided feed; no enumerated inventory validated |

### Citadel: new counted first-party source

The [official careers list](https://www.citadelsecurities.com/careers/open-opportunities/)
returned HTTP 200 to the repository's honest HTTP client, declaring 84 postings.
Its normal form publishes this read-only request:

```text
POST https://www.citadelsecurities.com/wp-admin/admin-ajax.php
Content-Type: application/x-www-form-urlencoded

action=careers_listing_filter
current_page=1
per_page=100
sort_order=DESC
jobTitle=
selected-job-sections=323,325,324,326
```

The four role-type IDs are the actual hidden default selection in the official
form, not inferred ATS tenant IDs. The response declares `number_of_post=84`,
`found_posts=84`, `post_per_page=100`, `page_number=1`; all 84 HTML cards parse
to unique first-party detail URLs with nonempty titles and locations. A second
live adapter fetch returned the identical 84 IDs in identical order. These are
the published careers-search listings, not the 88 sitemap URLs; the extra
sitemap entries are not silently treated as current openings.

India-location coverage: `systematic-trader`, `site-reliability-engineer-2`,
`site-reliability-engineer`, and `c-software-engineer` list Gurugram. The current
title filter matches only **C++ Software Engineer**. The readable Systematic
Trader detail independently confirms Citadel Securities as hiring organization
and Gurugram/IN in its JobPosting schema. It also explicitly describes potential
future vacancies: some listings are evergreen expressions of interest. A newly
observed listing must not be described as proof of a newly approved vacancy.

**Limits:** page 9 and the main frontend bundle still returned 403, despite
pages 1–2 and one detail succeeding. This is intermittent access, not a universal
WAF cure. The form POST succeeded twice without cookies, special UA, proxies or
challenge solving. Robots itself varied between 403 and a valid 200 ruleset
allowing the endpoint (`Disallow: /cdn-cgi/`, `Crawl-delay: 10`). The adapter
waits ten seconds after its robots check. Posting IDs are URL slugs, with
`req_id` blank; the application UUID seen on one detail is not invented for
other jobs. `datePosted` is omitted because the observed detail uses today's
date and does not establish original posting time. Category IDs may change;
the current four-category scope needs revalidation if the frontend changes.

### McKinsey: implementation and validation

Implemented the validated gateway source. Fresh global requests still return
595 unique jobs in the same order. All aligned city/country pairs are retained,
including India as a secondary location. Forty-six postings include India; the
current title filter matches **Principal Software Engineer I – Social,
Healthcare and Public Entities Tech**, job 110569, Bengaluru/Gurugram.

The adapter uses one 800-row request and deliberately fails closed on a larger
or truncated inventory instead of adding mutable pagination. It validates
count, IDs, friendlyURL identity, Avature application folder IDs and location
alignment. No posting date is inferred. Public detail-page availability remains
unverified from the local client; employer attribution rests on the first-party
gateway and matching McKinsey Avature links, as documented earlier.

### Gupshup and Blinkit: corrected diagnoses

Gupshup's published v2 JavaScript uses **POST**, not GET, for `job/alljobs`.
An unparameterized GET now returns 422 “no job found with that id”; that is not
evidence of a missing listings API. The normal public POST body
`{"page":1,"limit":10,"companyId":"main"}` returns an explicit successful
empty result. Company config independently names Gupshup and reports recruitment
disabled, onboarding enabled, `allJobsCount=0`. Do not claim there are no Gupshup
vacancies elsewhere. The first-party marketing careers CTA uses WhatsApp.

Blinkit's direct [jobs page](https://blinkit.com/careers/jobs) now returns 200.
Its published Drupal job-section JS calls `getTotalJobListing`, counts
`.row-job-listing` DOM nodes, and filters those nodes locally. The inspected HTML
contains zero such rows and explicitly displays zero results. This is stronger
than a search-cache snippet, but no nonempty posting shape has been validated;
neither source is enabled solely to turn an empty page into a success badge.

### Other automation can be partial

An additional feasible design is **importing job-alert emails the user chooses
to receive**, with deduplication and explicit source/coverage labels. LinkedIn
[documents daily/weekly job alerts and email delivery](https://www.linkedin.com/help/linkedin/answer/a511279/job-alerts-on-linkedin?lang=en).
That offers a route for Krutrim and potentially other employers when they post
there, without scraping LinkedIn pages. It needs one-time alert setup, authorized
mailbox access and a tested message parser; company-specific coverage is not
established. No subscriptions, mailbox access or outbound messages occurred.

Email alerts are incomplete discovery signals. Missing emails must never close
jobs or mark a complete company sweep. They would need a separate source model
or the existing incremental/no-closure contract, and should not be advertised
as equivalent to first-party inventory polling. An employer-authorized feed is
another possible route, but no permission or feed was obtained this session.

### Evidence and verification

- Network probes: `/private/tmp/jw-extensive-sep20/sep20-followup-*.json` and
  `sep20-candidate-live-baseline.json`; bodies are referenced by each manifest.
- Temporary runner baseline: `/private/tmp/jw-candidate-baseline-summary.json`:
  595 McKinsey + 84 Citadel jobs, both complete, null cursors, zero errors,
  zero notifications/export, all 679 rows suppressed. This is distinct from
  production activation.
- Production runner baseline: `/private/tmp/jw-production-recovered-baseline.json`:
  both complete under RunLock with 595/84 stored jobs, null cursor/error/backoff,
  zero notifications/export and all rows suppressed. Only then were the two
  config entries enabled. No manual publication or deployment was performed.
- Both adapters registered; nine new safety tests. **313 Python + 11 site
  tests pass**. Truncation, malformed identity/location, duplicates and HTTP
  failures cannot report a complete inventory.
- No exhaustive claim that every possible feed has been checked. WAF responses
  varied within minutes; old 403s do not establish permanent impossibility.

## Extensive feasibility analysis of all ten inactive companies — 20 September, 16:05 IST

User asked for an extensive analysis of whether the remaining blocked companies
can be automated. **Result: one of the ten is automatable and now has a
validated source — McKinsey. The other nine are not, and each is blocked for a
specific, separately-retriggerable reason.** No company was enabled and no
runtime code was changed by this analysis.

Method: the repository's own `watcher.http.HttpClient` and honest
`job-watch/0.1.0` User-Agent, 20-second timeouts, no retries, 1.2-second
pacing, `watcher.robots` checked before every target and on every redirect. No
challenge solving, browser impersonation, proxy, alternate-host bypass,
authentication, application, or notification. Search results were used for
discovery only, never as a job inventory. Evidence bodies and per-request
manifests: `/private/tmp/jw-extensive-sep20/`; helper `/private/tmp/jw_extensive.py`;
McKinsey logs `/private/tmp/jw-mckinsey-{gateway,pagination,search-html,coverage}.log`.
These are volatile diagnostics, which is why the findings are recorded here.

### Verdict table

| Company | Blocking reason (class) | Automatable now? |
| --- | --- | --- |
| McKinsey | none — complete counted first-party API found | **Yes, validated candidate** |
| Flipkart | publisher preference: `Disallow: /` | No |
| Ola | publisher preference: `Disallow: /` | No |
| Krutrim | project policy: only destination is LinkedIn Jobs | No |
| Gupshup | bot protection (Cloudflare 403) over a live portal | No |
| Blinkit | bot protection (Cloudflare 403) plus referral-only parent | No |
| Citadel Securities | bot protection; readable data lacks location | No |
| Zepto | upstream tenant not provisioned; WAF on own site | No |
| Instabase | employer publishes no live inventory (board removed) | No |
| Zomato / Eternal | employer publishes no inventory by design (referral-only) | No |

### McKinsey — recovered source, validated, not yet implemented

McKinsey's careers UI is backed by a counted public JSON search on **McKinsey's
own domain**, discovered through a public third-party integration and then
validated directly:

```
GET https://gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search?pageSize=800&start=1&lang=en
```

Anonymous: no key, cookie, login, Referer or special User-Agent. Validation:

- **Complete in one request.** `numFound` 595, `docs` 595, 595 unique `jobID` —
  declared equals returned equals unique, with no pagination gap to defend.
- **India cross-validated two independent ways.** `&countries=India` returns
  `numFound` 46 / 46 docs, and that set is *exactly* the 46 derived
  independently from the global response's per-record `countries[]` arrays:
  no missing rows, no extras.
- **Stable.** The global query repeated 12 minutes later returned the identical
  595 IDs **in identical order**, and India 46 both times.
- **Location data is sound.** `cities[]`, `countries[]` and `continents[]` are
  equal-length and positionally aligned on every one of the 595 records, so
  India cities resolve per job (Gurugram 40, Chennai 22, Bengaluru 17,
  Mumbai 7, Kolkata 4). Every India record yields at least one India city.
  Forty are India-only postings; six are multi-country postings that include
  India, which is the same multi-location case Siemens and Uber already handle.
- **Required fields are present on 595/595:** `jobID`, `title`, `cities`,
  `countries`, `friendlyURL`, `jobApplyURL`.
- **Pagination semantics established:** `pageSize` plus a **one-based page
  index** `start` (`start=2&pageSize=10` returned exactly global rows 11–20).
  `rows=` is not a valid parameter and returns 400.
- **Robots.** `www.mckinsey.com/robots.txt` is served and allows `/careers/...`
  (its `Disallow: /search` is a different path prefix). The gateway host's own
  `/robots.txt` is itself a Cloudflare 403, so **no rules are delivered**;
  `watcher.robots.fetch` maps every HTTP error to an empty ruleset, which is
  what RFC 9309 specifies for an unavailable-status robots file and is the same
  situation already accepted for Uber (404). The API itself answers a clean
  `200 application/json` with no challenge header.

Honest gaps, all of which are engineering/verification work rather than access
barriers:

1. **Public detail pages are unreachable.** `www.mckinsey.com/careers/search-jobs`
   and two constructed `/careers/search-jobs/jobs/<friendlyURL>` details fail at
   the TCP level (`Errno 60`, ~18s) even with a 75-second timeout, while
   `robots.txt` on the same host succeeds. So employer identity cannot be
   confirmed from a detail page. It rests instead on the data being served from
   `gateway.mckinsey.com` — a first-party McKinsey host — with every
   `jobApplyURL` pointing at McKinsey's own Avature tenant
   (`mckinsey.avature.net/careers/ApplicationMethods?folderId=<jobID>`) and
   McKinsey's own careers taxonomy in `interestCategory` / `jobSkillGroup`.
   This is first-party attribution, not an ATS tenant guess, but it is weaker
   than the detail-page confirmation used for LinkedIn, Uber and Siemens.
2. **No reliable posting date.** The only date-like field is
   `postedToLinkedInDate`, present on 483/595. `posted_at` is a free-text
   column, so an empty value is storable, but no "new since" date is available.
3. **The base path `apigw-x0cceuow60` looks like a deployment identifier** and
   could rotate, the same fragility already documented for Meta's persisted
   query IDs. An adapter should fail closed on response shape.

Implementation path is short and has a direct precedent: this is the same
single-oversized-request pattern as `watcher/adapters/bain.py` (326 rows in one
400-row response), so a ~120-line `watcher/adapters/mckinsey.py` plus tests,
then a silent production baseline before flipping `config.yaml`, matching how
LinkedIn, Uber and Siemens were enabled today. **Not started: enabling a new
company adds recurring third-party traffic, so it waits on your go-ahead.**

### The nine that are not automatable, and why each is distinct

**Publisher preference — Flipkart and Ola.** Re-confirmed today with the
repository's own robots matcher: `flipkart.turbohire.co/robots.txt` and
`olacareers.turbohire.co/robots.txt` are both **served 200** with an explicit
`User-agent: * / Disallow: /`, and `Robots.allowed()` returns False with
`why → "Disallow: /"`. This is the publisher stating a preference, not a
technical failure — the boards are reachable and deliberately not polled. No
board path was fetched. Flipkart's permitted first-party `/jobslist` remains a
static ten-card mock with a hard-coded 69-opening heading and a Show More that
only alerts. Retrigger: a robots change, or a real first-party feed.

**Project policy — Krutrim.** The official AI Labs site's own bundle opens
`linkedin.com/company/krutrim/jobs/` and nothing else; `/careers` still 404s.
The only verified destination is the LinkedIn Jobs platform, which the build
spec excludes. Ola's shared TurboHire org does not establish Krutrim
membership. Retrigger: a first-party or ATS board of its own.

**Bot protection over a live portal — Gupshup.** Both Darwinbox candidate apps
load, and their current bundles identify the exact read-only routes
(`getCompanyConfig`, `job/alljobs`, `job/filters`, plus legacy `companyinfo`
and `job?page=1`); every one returns a Cloudflare 403. Clarification from this
run: `gupshup.darwinbox.in/robots.txt` "200" is **not a robots file** — it is
the SPA's HTML shell, so there are no robots rules at all. Gupshup is therefore
purely a WAF block, never a policy block. The official `gupshup.ai/careers`
CTAs route candidates to WhatsApp. Retrigger: the API becoming readable.

**Bot protection plus referral-only parent — Blinkit.** The robots-permitted
`blinkit.com/careers/jobs` returns a branded Cloudflare 403 containing no job
API lead; `jobs.blinkit.com` also 403s and identifies as a Vendor Dashboard.
Its parent Eternal is referral-only (below). Do not infer zero jobs from a 403.

**Readable but unusable data — Citadel Securities.** New today: the sitemap
index is readable and exposes `career-sitemap.xml`, which returns **200 with 88
`/careers/details/<slug>/` URLs**. That is the most that is obtainable —
`robots.txt` and `/careers/open-opportunities/` both return 403 (WAF), so
detail pages cannot be read. The sitemap carries **no job IDs, no titles beyond
the slug, and no locations**; all 88 `lastmod` values are today's bulk
regeneration (09:22Z), so they are not per-posting change signals; and zero
slugs name any India city, while several are evergreen campus-referral pages
with regional suffixes (`-asia`, `-us`). Since this watcher filters to India, an
inventory with no location field cannot be filtered honestly. Retrigger:
readable details, or a JSON endpoint.

**Upstream tenant not provisioned — Zepto.** Decisive new evidence: Zwayam's
own public configuration API is robots-allowed and answers cleanly, and it
**rejects both documented Zepto hostnames**:

```
POST https://public.zwayam.com/data-service/v2/public-configurations  (multipart companyUrl)
  zeptonow.openings.co           → 403 {"message":"Invalid CompanyUrlzeptonow.openings.co"}
  zeptonow.cluster3.openings.co  → 403 {"message":"Invalid CompanyUrlzeptonow.cluster3.openings.co"}
```

That is the platform itself saying the tenant does not exist, not bot
protection. The `cluster3` host's shell is a Next.js static export whose
`__NEXT_DATA__` has empty `pageProps` and contains no company identifier, so no
valid mapping can be read from it; its `TechCorp Careers` label and
Flipkart/preprod fallbacks are defaults and must not be used as identity.
Zepto's own careers path still answers an AWS WAF challenge and the officially
linked TalentRecruit host still 403s. Retrigger: a newly discovered platform,
or a valid tenant mapping.

**No live inventory — Instabase.** Its careers page and sitemap are both fully
readable, which makes the finding conclusive rather than obstructed: the page's
JS still hard-codes Greenhouse board `instabase` (`boards-api.greenhouse.io/v1/boards/instabase`,
`job-boards.greenhouse.io/instabase/jobs/8042464002` and `8073772002`), and that
board and both details 404. The sitemap contains exactly three careers URLs
(`/careers`, `/careers/jobs`, `/careers/jobs/recruitment-fraud-alert`) and **no
individual job pages at all**, and the page references no other ATS. The
employer currently publishes no reachable inventory. Retrigger: the Greenhouse
board returning 200, or a new ATS appearing in that page.

**No inventory by design — Zomato / Eternal.** `eternal.com/careers` and its
current careers component accept applications only through employee referrals
and make no listing request. The SmartRecruiters `Zomato1` board is real but
abandoned (rows from 2015–2016; one still accepts applications, which does not
make it a current inventory). This is the employer's stated hiring process, not
an obstacle to work around. Retrigger: a published openings list.

### What this does and does not claim

It claims: nine of ten cannot be automated today, for the specific reasons
above, and McKinsey can. It does **not** claim permanence — every row has a
concrete retrigger condition, and three of them (Gupshup, Blinkit, Citadel)
are live portals whose data exists and is merely unreadable by an honest
client. It does not claim every possible source has been discovered.

Baseline checks after this analysis: 304 Python tests and 11 site tests pass,
and `config.yaml` loads through `watcher.config.load` at 141 tracked /
131 active / 10 inactive with Qualcomm's `cursor_max_age: 86400` in effect.

## Current recovery status — 20 September, 15:31 IST

**LinkedIn, Uber, and Siemens are enabled. There are 10 inactive companies.**
LinkedIn's permitted SmartRecruiters HTML source completed a silent 57-job
baseline before activation and has since completed normal scheduled polls.
Its 22 matches are verified in the live 131-company snapshot. The restriction
on scraping the LinkedIn Jobs platform remains; the separate SmartRecruiters
postings API was not used because its robots disallows crawling.

The remaining inactive companies are Gupshup, Krutrim, Flipkart, Zepto, Zomato,
Blinkit, Instabase, Citadel Securities, McKinsey, and Ola. The 11:50 handoff
records fresh evidence for all ten. No current, complete replacement inventory
was established for them; this is not a claim that recovery is impossible.

Zepto's `zeptonow.cluster3.openings.co` serves a Zwayam frontend, but its
published public-configuration request rejects that company URL. Generic
TechCorp/Flipkart fallback configuration is not Zepto identity. Gupshup's
additional legacy company-info and job-list routes returned 403. Instabase's
newly discovered Greenhouse detail 8783218002 and board still returned 404.
Citadel's listing remained 403, McKinsey search timed out, and Blinkit's jobs
route returned a branded error page. Flipkart/Ola TurboHire robots still
disallows the watcher. Krutrim still links LinkedIn, and Eternal exposes its
referral path without a verified enumerable inventory.

See [SESSION_HANDOFF.md](SESSION_HANDOFF.md) for exact evidence and operational
follow-up. Older blocked/disabled claims about recovered employers below are
historical and superseded.

## Siemens recovery confirmed — 20 September, 11:31 IST

**Siemens is now enabled**, superseding all incomplete-baseline wording below.
Its existing bounded process completed at 11:28:56 with 939 unique jobs, no
cursor/error/backoff, and all notifications suppressed. Identity, title,
location, URL, and complete production membership were verified before enabling.
Eighty-two jobs pass the configured filters and are included in the verified
130-company live snapshot. No diagnostic process remains. Uber also remains
enabled. There are now 11 inactive companies; see [SESSION_HANDOFF.md](SESSION_HANDOFF.md)
for current operational state and outstanding Microsoft/normal-poll verification.

## Recovery confirmed — 20 September, 11:18 IST

**Uber is now recovered and enabled**, superseding the candidate/blocked
wording below. Its public Oracle CX_1 inventory has 531 global IDs, exactly
matching the official sitemap; all 20 India results match the global inventory's
India locations. Identity/details were validated and a complete silent
20-job production baseline succeeded before activation. Three roles match the
current filters, and the published site now includes 129 active companies.

Siemens remains disabled while a bounded baseline is running (432/939 at
11:17). Do not start a duplicate. See [SESSION_HANDOFF.md](SESSION_HANDOFF.md)
for current processes, implementation changes, evidence, tests, and remaining
work. Citadel's `/wp-json/` discovery endpoint also returned 403 on 20 September.

## Subsequent findings — recorded 20 September

These findings supersede the initial check's limits below. The user then
prioritized Microsoft and the live company list; complete latest operational
state is in [SESSION_HANDOFF.md](SESSION_HANDOFF.md).

- **Uber has a new recovery lead:** anonymous HTTP 200 from Oracle host
  `iaziqy.fa.ocs.oraclecloud.com`, recruiting site `CX_1`, returned 20 India
  jobs (declared count 20), including Uber for Business titles. Robots was 404.
  Discovered via public integration source; official employer attribution and
  complete India/secondary-location coverage still need verification before
  baselining/enabling. Existing Oracle parser omits secondaryLocations[].Name,
  which this response uses. Evidence: `/private/tmp/jw-uber-oracle-probe.log`
  and `/private/tmp/jw-recovery-review/uber-oracle.json`.
- **Siemens** now uses server-echo-validated `postedDate:ASC` sort. One silent
  production baseline was deliberately stopped when priorities changed, with
  102 IDs retained and cursor offset 108, pending 6, total 953. Still disabled,
  last_complete absent. This is incomplete validation, not access blocking.
- **Citadel Securities:** later fetched the sitemap successfully using the
  repository's robots semantics; the earlier robots-403 stop is not proof
  that the sitemap is forbidden or unavailable.
- **Zepto:** modern Python/OpenSSL removes the legacy TLS error but the
  TalentRecruit robots request still returns 403; no recovered inventory.
- Gupshup's exact public inventory/config/filter APIs still return 403;
  Instabase's linked Greenhouse endpoint still returns 404; McKinsey search
  still times out. No inactive company has been enabled.

Do not claim that all permitted recovery routes are exhausted: the new Uber
lead and Siemens completion are concrete unfinished work.

The user clarified that the task is to investigate permitted recovery routes,
not to add a permanent lock against enabling companies.

## Conclusion and limits

This review did **not establish a new production-ready recovery** for the twelve
inactive companies other than policy-excluded LinkedIn. It does **not** prove
that recovery is impossible or that every possible source has been discovered.
In particular, Siemens has a readable, previously validated adapter: completing
a coherent production baseline remains an engineering/operational problem, not
an access prohibition. Do not describe Siemens as impossible to unblock.

Checked the concrete leads in SESSION_HANDOFF.md, official search-discovered
careers destinations, linked frontend code, public inventory APIs, sitemaps,
RSS, and a Siemens jobs widget discovered in its sitemap. Search results were
used for discovery only, never as a current job inventory. No broad ATS tenant
guessing was repeated. Existing evidence for dead, unrelated, or abandoned
boards was reviewed rather than repeatedly fetching those boards.

Direct checks used `watcher.http.HttpClient`, the honest watcher User-Agent,
20-second timeouts, no retries, and one-second pacing per investigation. Robots
were checked before target requests, including redirects. Missing robots files
and successful responses containing no applicable rules were not invented into
prohibitions. Robots 403s and network failures stopped dependent target fetches.
There was no challenge solving, browser impersonation, proxy, alternate-host
bypass, notification, application, production baseline, or deployment.

## Findings from direct checks

Checks began at 16:03 UTC (21:33 IST). Numbers are observations, not constants.

| Company | Routes and evidence | Recovery status |
| --- | --- | --- |
| Uber | [Official listing](https://jobs.uber.com/en/) and a detail actually present in its sitemap (`/en/jobs/149574/`) returned Cloudflare challenge 403s. [Job sitemap](https://jobs.uber.com/en/jobs/sitemap.xml) returned 531 URLs, with IDs and lastmods but no title/location fields. | Sitemap plus readable details remains a concrete potential recovery; direct detail access still fails. A sitemap alone is insufficient. |
| Gupshup | [Official careers](https://www.gupshup.ai/careers) still links WhatsApp. Both [legacy candidate app](https://gupshup.darwinbox.in/ms/candidate/careers) and [v2 app](https://gupshup.darwinbox.in/ms/candidatev2/main/careers) load. Current JS establishes `/ms/candidateapi/` and the routes below. Exact read-only config, inventory POST, and filters returned Cloudflare 403s. | The portal is live; its data APIs are inaccessible to the watcher. Do not call the portal switched off. |
| Krutrim | [AI Labs](https://ai-labs.olakrutrim.com/) loads. Its current landing-page JS explicitly opens `https://www.linkedin.com/company/krutrim/jobs/`. Old `/careers` is still 404. | The verified destination is excluded by project policy. No independent permitted inventory was established; Ola's shared board does not prove Krutrim membership. |
| Flipkart | The official home response links its TurboHire organization. [First-party jobslist](https://www.flipkartcareers.com/jobslist) is readable but still has the hard-coded 69-opening heading, ten static cards, and a Show More function that only alerts. [Board robots](https://flipkart.turbohire.co/robots.txt) disallows the watcher. | No board path fetched. The first-party mock is not a recovery. |
| Zepto | [Official careers](https://www.zepto.com/s/careers) returned HTTP 202, zero body bytes, and `x-amzn-waf-action: challenge`. The officially linked TalentRecruit host's robots request failed TLS negotiation with `TLSV1_ALERT_PROTOCOL_VERSION`; its board was consequently not fetched in this review. | Access remains unresolved. This qualifies the earlier handoff's claim that the TLS failure had stopped recurring; do not report a new board 403 from this run. |
| Zomato / Eternal | [Careers](https://www.eternal.com/careers) and its current `careers.C7YyDy5S.js` component load. The component explicitly states employee-referral-only applications and exposes no listing request. Previously inspected old SmartRecruiters and unrelated Lever inventories provide no new lead. | No permitted enumerable inventory established. Referral-only is the official observed application path, not proof there can never be a future board. |
| Blinkit | [Careers listing](https://blinkit.com/careers/jobs), the search-discovered `/careers/node` route, and `jobs.blinkit.com` returned 403s. The inspected listing response is a branded Access Denied page, not a job inventory. It contains no job API lead. | Technical refusal remains. Do not infer zero jobs, and do not identify this response as a specific Cloudflare challenge without corresponding evidence. |
| Instabase | [Jobs page](https://www.instabase.com/careers/jobs) and [company page](https://www.instabase.com/company) are readable. Current jobs-page JS still embeds Greenhouse `instabase`. Both the base jobs API and exact frontend `jobs?content=true` endpoint returned 404, as did linked detail 8042464002. | The official integration is still broken. Old placeholder cards and prior dead details are not a replacement inventory. |
| Siemens | [India search](https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?42386=%5B812053%5D&42386_format=17546&listFilterMode=1&folderRecordsPerPage=6&folderOffset=0) returned six rows of 953. The linked RSS contains 20 items, empty descriptions, no locations or declared total, and starts with 2019/2020 dates. The robots-advertised locale sitemap contains 58 static routes and zero concrete job URLs. Its [Open Positions widget](https://jobs.siemens.com/en_US/externaljobs/WidgetOpenPositions) has 21 distinct detail links and an Explore all link back to search, without full-inventory evidence. Visible sort choices are Most recent, Field of work, and Job title, not job ID. | The counted HTML adapter remains the concrete recovery route. Neither RSS, sitemap nor widget establishes a complete substitute. No new full sweep or production baseline was attempted. A quieter bounded baseline or a tested coherence design remains possible and unproven. |
| Citadel Securities | [Robots](https://www.citadelsecurities.com/robots.txt) now returns 403. Dependent listing/sitemap/detail fetches were stopped. Earlier evidence remains: readable 88-slug sitemap but challenged details, no standalone title/location inventory. | This run cannot reconfirm the earlier sitemap count or current detail status. Recheck robots/access before further direct investigation; absence of a usable route is not proved permanently. |
| McKinsey | Robots is readable and permits careers. [Main job search](https://www.mckinsey.com/careers/search-jobs) timed out without a response. [Candidate portal](https://jobs.mckinsey.com/) loads but its Open positions link points back to that main search. Earlier search/detail/sitemap timeouts and indexed job snippets do not supply a pollable inventory. | Direct access remains unresolved. Candidate-account pages are not an alternative listing. No repeated main-host timeout probes were run. |
| Ola Mobility / Electric | Both [Mobility careers](https://www.olacabs.com/careers) and [Electric careers](https://www.olaelectric.com/careers) load and link organization `e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212`, through different TurboHire routes. [Board robots](https://olacareers.turbohire.co/robots.txt) still disallows the watcher. | No TurboHire board path fetched. No first-party inventory found in these pages; no inference about Krutrim. |

Gupshup requests were derived from the current frontend, with ordinary
Origin/Referer and English language headers, no authentication or challenge data:

- `GET /ms/candidateapi/getCompanyConfig?is_preview=false&companyId=main`
- `POST /ms/candidateapi/job/alljobs`, JSON `{"page":1,"limit":10,"companyId":"main"}`
- `GET /ms/candidateapi/job/filters`

The POST is a read-only inventory search, not an application or account action.

## Evidence and next steps

Local response bodies and per-company request records are in
`/private/tmp/jw-recovery-review/`; the probe helper is
`/private/tmp/jw_recovery_review.py`. These are temporary diagnostic artifacts,
not production code or permanent fixtures. This document preserves the findings
if the temporary directory is cleared. Earlier source evidence is retained in
SESSION_HANDOFF.md and AUTOMATION_AUDIT.md.

Do not retry failures in a tight loop. Useful future triggers remain a readable
official API/detail route, changed robots permission, a repaired Instabase
integration, or a defensible Siemens baseline/coherence design. An untested
engineering possibility must remain labeled unresolved; it is not proof of
either recovery or impossibility.

No runtime code or company activation settings changed. Config remains
141 tracked, 128 active, 13 inactive. Baseline verification passed 280 Python
tests, 11 site tests, and `git diff --check` before this documentation update.
