# ATS endpoints — what they actually return

Everything below was confirmed with a live request on **2026-09-16** from a
residential IP with the tool's own `User-Agent`. Shapes are what the endpoints
returned that day, not what their documentation claims.

## Conditional requests (ETag)

Checked by sending back the `ETag` the endpoint had just given:

| ATS | `ETag` | Conditional GET | Notes |
| --- | --- | --- | --- |
| Greenhouse | yes, weak | **304**, 0 bytes | `cache-control: max-age=0, private, must-revalidate` |
| Lever | yes, weak | **304**, 0 bytes | |
| Ashby | yes, weak | **304**, 0 bytes | `cache-control: public, max-age=60` |
| SmartRecruiters | yes, weak | **304**, 0 bytes | per page; only trusted when the whole board fits on page 0 |
| Workday | **no** | n/a | `cache-control: no-store, no-cache`; `If-Modified-Since` returns 200 |
| Eightfold | **no** | n/a | `cache-control: no-store` |
| Jibe | **no** | n/a | one request per location, so still 15-minutely |
| amazon.jobs | yes, weak | **304**, 0 bytes | per page |
| Atlassian | yes, weak | **304**, 0 bytes | whole board is one 1.9MB response |
| Rippling | **no** | n/a | `cache-control: no-store`; one request, so still 15-minutely |
| Oracle Recruiting Cloud | **no** | n/a | `cache-control: no-store` |

So Greenhouse/Lever/Ashby polls mostly transfer nothing and stay at 15 minutes.
Workday cannot revalidate and pages 20 rows at a time, hence the 1-hour default.

## Greenhouse

`GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`

Whole board in one response: `{"jobs": [...], "meta": {"total": n}}`. Per job:
`id` (per-posting, used as `job_id`), `requisition_id` (kept as `req_id`),
`title`, `location.name`, `offices[].name`, `absolute_url`, `first_published`,
`updated_at`, `content` (HTML, kept in `raw`).

`https://job-boards.greenhouse.io/{token}` and `boards.greenhouse.io/embed/job_board?for={token}`
carry the same token; detection recognises all three spellings.

## Lever

`GET https://api.lever.co/v0/postings/{token}?mode=json`

A bare JSON array, whole board, no pagination parameters needed. Per posting:
`id` (uuid), `text` (the title), `categories.location`, `categories.allLocations`,
`createdAt` (epoch ms), `hostedUrl`, `workplaceType`. No requisition number.

## Ashby

`GET https://api.ashbyhq.com/posting-api/job-board/{token}`

`{"jobs": [...], "apiVersion": ...}`, whole board. Per job: `id` (uuid), `title`
(**leading whitespace happens** — trimmed), `location`, `secondaryLocations[].location`,
`isRemote`, `isListed` (false ones are skipped), `publishedAt` (ISO), `jobUrl`.

## SmartRecruiters

`GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N`

`limit` maxes at 100. `{"totalFound": n, "content": [...]}`; page until
`offset >= totalFound`. Per posting: `id`, `name`, `refNumber` (→ `req_id`),
`releasedDate`, `location.fullLocation` / `city` / `country` / `remote`. The
response carries **no job URL**; it is built as
`https://jobs.smartrecruiters.com/{token}/{id}`.

**Trap:** this API answers **200 with `totalFound: 0`** for any token at all,
real or invented. A 200 means nothing on its own — detection treats an empty
board as a miss.

## Workday

`POST https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
with `{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}`.

Confirmed against `salesforce.wd12` (1,465 reqs) and `rbs.wd3` (89):

* **`limit` above 20 is an error**, not a truncation: `limit: 50` → HTTP 400.
* **Pagination wraps.** `offset=100` on an 89-row board returns page one again,
  not an empty page. Stopping on "empty page" never stops.
* **`total` is only on the offset-0 response.** Later pages report `total: 0`,
  so the count must be read once and kept. (Recorded in the fixtures.)
* **`bulletFields` order is tenant-specific.** Salesforce: `["JR344521"]`.
  NatWest: `["Hybrid", "Closing Date: 20/09/2026", "R-00284752"]`. Taking `[0]`
  collapses a whole board onto a handful of ids. The req ID is picked as the
  bullet field that also appears in `externalPath`.
* `externalPath` ends in `_<ReqID>[-N]`; that suffix is unique per posting and is
  used as `job_id`.
* **Multi-location reqs say `"3 Locations"`** in `locationsText` instead of naming
  them. `GET {api}/{site}{externalPath}` returns `jobPostingInfo` with `location`,
  `additionalLocations[]`, `jobReqId`, `startDate` — fetched for new
  title-matching rows only (≤10 per company per run). If that lookup can't run,
  the vague row is treated as a match rather than dropped.
* **`total` is approximate.** Walmart reported 2,000 and delivered 1,986 unique
  postings; the adapter accepts a sweep within 5% of `total` (logging a warning)
  and treats anything shorter as a failed fetch rather than mass-closing reqs.
* `postedOn` is relative text ("Posted Today", "Posted 30+ Days Ago"), not a date.
* **No sort parameter, no posted-date facet** (facets are job family, time type,
  worker sub-type, location). But on some tenants the *default* order is
  newest-first, checked 2026-09-18 by sampling `postedOn` at offsets 0 / 200 /
  600: **yes** on `wf` (Wells Fargo; also identical across two reads),
  `salesforce`, `paloaltonetworks`; **no** on `walmart` (1 day, 30 days, 1 day)
  and `nvidia` (3, 3, 2, 3). On a newest-first tenant, `incremental: true` reads
  pages only until two in a row hold nothing new (a normal Wells Fargo poll: 3
  requests instead of 91), checks on every page that ages never go down (and
  sweeps instead if they do), and never retires anything; a complete sweep
  still runs every `full_sweep` seconds (default 12h), because only a complete
  sweep can see that a req closed.
* Tenant, shard (`wd1`…`wd504` — Walmart really does use a three-digit one) and
  site slug cannot be guessed; they come from a real posting URL.
* A tenant may publish several sites; `sites:` takes a list and postings shared
  across sites are deduped.
* Site slugs are not guessable from the company: PayPal's is literally `jobs`
  (`paypal.wd1` → `jobs`) and eBay's is literally `apply` (`ebay.wd5` → `apply`),
  while `External`, `Careers`, `PayPalGlobal`, `eBayCareers` and friends all 404.
* Where a careers site hides its Workday, its **front end often leaks it**. eBay's
  `careers.ebayinc.com` shows no ATS at all, but its Phenom People search API
  (`POST https://jobs.ebayinc.com/widgets`, `ddoKey: refineSearch`) returns jobs
  whose `applyUrl` is `ebay.wd5.myworkdayjobs.com/apply/job/…` — tenant, shard and
  slug in one string. Worth trying on any Phenom-fronted careers site.

## Eightfold (Microsoft)

`GET https://{host}/api/pcsx/search?domain={domain}&query=&location={location}&start=N&num=10`

Multi-tenant, but the host is per company (`apply.careers.microsoft.com`) and the
tenant is named by `domain`. Results are nested under `data`:
`data.count` (total for the query, present on every page) and `data.positions[]`
with `id`, `displayJobId` (the requisition number a referral needs), `name`,
`locations[]`, `standardizedLocations[]`, `postedTs` (epoch seconds),
`workLocationOption`, `positionUrl`.

* **`num` is capped at 10** — asking for 50 or 100 still returns 10. Microsoft's
  India board is ~220 positions, so ~23 requests per sweep.
* `start` past the end returns an empty list. No wrap-around.
* Pages advance by rows returned, not by `num`, so a short page cannot skip rows.
* `location` filters server-side, which is the point: unfiltered, Microsoft's
  board is thousands of rows at 10 a page. One query per configured location,
  deduped by position id.
* **It rate-limits by volume, not by spacing.** A 23-request sweep at 0.5s gaps
  returned 429 at request 10; repeating it at 2s and then 6s gaps still hit 429
  after 4-5 requests, while single requests kept working. The limit behaves like
  a per-IP quota over a window of tens of minutes, so no polite delay avoids it
  once the quota is spent — Microsoft is configured two-hourly with 6s gaps, and
  the persisted backoff handles the rest. Expect occasional `BACKED-OFF microsoft`
  lines in the log; the sweep resumes from its cursor afterwards.
* **Partial sweeps are the normal case here.** Because the quota bites well
  before a 23-request sweep finishes, the adapter returns the rows it did get
  along with a cursor instead of raising, and the runner records them, keeps the
  cursor, and parks the company for 30 minutes. Observed live: 79 rows on the
  first run, 40 more on the next, and so on until the sweep completes. A park
  that follows real progress does **not** escalate the backoff — otherwise a
  board that always throttles mid-sweep would climb to the 24-hour ceiling while
  it was in fact working.
* The older `/api/apply/v2/jobs` flavour exists on some tenants; on Microsoft it
  answers `{"message": "Not authorized for PCSX"}`, and on a host that is not the
  tenant's own it answers `"Tenant not identified"`. There is no single Eightfold
  URL that works everywhere — host and domain must both come from the real board.

## amazon.jobs

`GET https://www.amazon.jobs/en/search.json?result_limit=100&offset=N&sort=recent&country=IND`

Self-hosted but public and keyless. `hits` is the total (capped at 10000
unfiltered; 2,325 for India), `jobs[]` carries `id_icims` (requisition number),
`title`, `location` ("IN, KA, Bengaluru"), `posted_date` ("September 16, 2026",
normalised to ISO here), `job_path`.

* `result_limit` maxes at 100; **200 returns `{"jobs": null}`**, not an error.
* **`country=IND` filters — `country[]=IND` is silently ignored**, returning the
  global board while looking like it worked. The bracketed form is what the
  site's own UI sends, so this is easy to ship by accident; there is a test
  asserting the request we send is the unbracketed one.

## Atlassian

`GET https://www.atlassian.com/endpoint/careers/listings` — 285 listings in one
response, with an ETag.

Atlassian runs **iCIMS**, and iCIMS itself is not queryable here:
`globalcareers-atlassian.icims.com/jobs/search` answers HTML for every variation
tried (`?format=json` and `.rss` both return the same 470KB page, and the plain
search URL just bounces back to atlassian.com). Their own endpoint publishes the
same board as JSON, so that is what the adapter reads. Each listing has `id`
(the iCIMS job id and the req number), `title`, `locations[]`, `category`,
`applyUrl`, and `portalJobPost.portalUrl` / `.updatedDate`.

## Rippling ATS

`GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs`

Multi-tenant (the public board is `ats.rippling.com/{token}/jobs`), keyless, and
the whole board is one bare JSON array. Only five fields per row: `uuid`, `name`,
`department`, `url`, `workLocation` — **no posted date and no requisition
number**, so ranking falls back to first_seen and a referral gets only the URL.

* **One row per (job, location).** Rippling's 647 rows are 334 distinct `uuid`s;
  a posting open in 20 US states appears 20 times. Keeping the first row and
  dropping the rest loses locations — on the day this was written, exactly one
  posting had a US state first and a Bangalore row later, so first-wins would
  have hidden an India job. Rows are merged by `uuid`, locations joined.

## Oracle Recruiting Cloud (JPMorgan Chase)

`GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions`
`?onlyData=true&expand=requisitionList.secondaryLocations`
`&finder=findReqs;siteNumber={site},limit=200,offset=N,sortBy=POSTING_DATES_DESC,location={location}`

Public, keyless, and multi-tenant — every `*.fa.oraclecloud.com` careers site
shares this shape, so this adapter should pick up other banks and enterprises.
The awkward part is `finder=`: a pseudo-query whose parameters are
comma-separated *inside* it rather than being normal query parameters.

Verified against `jpmc.fa.oraclecloud.com` / `CX_1001` on 2026-09-16:

* The payload is `items[0]`, holding `TotalJobsCount` and `requisitionList[]`.
* **`expand=requisitionList…` is mandatory.** Without it the response still
  carries a plausible `TotalJobsCount` but no `requisitionList` at all — which
  reads exactly like an empty board and would mass-close every stored req. The
  adapter raises instead, and there is a test pinning that.
* `limit=200` works. Unfiltered that is 7,394 reqs (37 requests); `location=India`
  narrows it to 314 (2 requests), which is why the location filter is configured.
* Per req: `Id` (the requisition number, also the apply URL), `Title`,
  `PostedDate`, `PrimaryLocation` ("Mumbai, Maharashtra, India"),
  `secondaryLocations[]`, `JobFunction`.
* **An unrecognised `location` is silently ignored, not rejected.** On Oracle's
  own site (`eeho.fa.us2` / `CX_45001`), `location=Bengaluru` returns the entire
  2,165-job global board while looking filtered; only `India` narrows it (to 41,
  which a 400-row scan of the unfiltered board agrees with). The adapter samples
  the first page and logs a warning when none of the rows mention the configured
  location, so a silently-ignored filter cannot pass for a working one.
* Apply URL: `https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{Id}`.

## Jibe career sites (DocuSign)

`GET https://{host}/api/jobs?limit=100&page=N&location={location}` (page is 1-based)

DocuSign runs **iCIMS** underneath (`apjcareers-docusign.icims.com` and friends),
which as with Atlassian is not queryable. Its careers front end is Jibe (assets
from `app.jibecdn.com`) and that does expose plain JSON on the careers host.

Verified on `careers.docusign.com`, 2026-09-16: `totalCount` 245 board-wide, 77
with `location=India`, `limit=100` honoured. Per job: `slug`, `req_id` (the
requisition number), `title`, `location_name` ("IN-Bangalore"), `country`,
`categories[]`, and a multi-KB `description` that is dropped rather than carried
in memory for every row.

## Radancy / TalentBrew via sitemap (Intuit)

`GET https://jobs.intuit.com/sitemap.xml`

Intuit has no JSON board: `/search-jobs/results` returns a JSON envelope whose
`results` field is a blob of HTML. The sitemap is a real machine-readable feed,
and every job URL carries the fields needed:
`/job/{city}/{title-slug}/{clientId}/{requisitionId}` plus `<lastmod>`.
512 job URLs, 34 in Bengaluru, `lastmod` present on all of them.

This is a weaker source than an API and is typed as such: titles are
**slug-derived** (casing and punctuation lost — filters normalise anyway),
location is a **bare city slug** with no country, `lastmod` is last-changed not
first-posted, and coverage is only what the sitemap lists — Intuit's own search
implies more open reqs than 512. No ETag on a ~330KB file, so it runs hourly.
Verified only on Intuit: `careers.walgreens.com` and `jobs.cvshealth.com` publish
nothing comparable, so this does not automatically generalise to TalentBrew.

## Checked and still not reachable

| Company | Why |
| --- | --- |
| Google | No public JSON endpoint found: `careers.google.com/api/v3/search/` and the `google.com/about/careers/applications/api/…` paths all answer 404 |
| Uber | `uber.com/careers` answers **406** to an honest User-Agent, and the old `/api/loadSearchJobsResults` RPC handler no longer exists |
| Flipkart | SPA; no API reference in the served HTML, and `flipkartcareers.com/api/v1/jobs` 301s away |
| Nutanix | `careers.nutanix.com` is behind a Cloudflare challenge, and `nutanix.eightfold.ai` 404s |
| Apple | `jobs.apple.com` is a JS app; `/api/role/search` 301s to a not-found page and no endpoint appears in the served HTML |
| Meta | `metacareers.com` GraphQL needs a `doc_id` plus a session token; answers 400 without them |
| Myntra | `careers.myntra.com` redirects to a 2.5KB `jobs.myntra.com` SPA shell; no API in the served HTML or its bundles |
| Chargebee | No board at all — `chargebee.com/careers` sends applicants to LinkedIn |
| ~~Nykaa~~ | polled since 2026-09-18 by the Skima HTML adapter (see below); no API exists |
| Hasura, Gupshup, D. E. Shaw | jobs embedded in a Next.js `__NEXT_DATA__` payload — reading that on every poll is HTML scraping |
| Zepto, BookMyShow, Blinkit, Qdrant, Krutrim, Vespa | careers host 403s, 404s, fails DNS, or rejects our TLS handshake |
| Flipkart, Zomato, PhonePe, LinkedIn, ScyllaDB, Quadeye | nothing job-shaped in the served HTML at all |

Revised 2026-09-18 (evening), superseding the rows above where they disagree:
**Media.net** is polled from its HTML (below). **Zepto** is bot protection, not
server rendering: every careers path answers 202 with `x-amzn-waf-action:
challenge`. **Zomato**: `eternal.com/careers` publishes no openings at all, only
"find someone who can vouch for you" and `mailto:hr@eternal.com`. **Blinkit**'s
board does exist, at `jobs.blinkit.com`, and blocks automated clients at
Cloudflare, headless Chromium included. Bot protection is not worked around, so
Zepto and Blinkit stay closed.

A second pass read each site's same-origin JavaScript bundles looking for an API
base, and probed `/api/jobs`, `/api/v1/jobs`, `/jobs.json`, `/wp-json/wp/v2/jobs`,
`/sitemap.xml` and friends on every one of them. The only job-bearing feed found
anywhere was Intuit's sitemap. PhonePe, ScyllaDB, Gupshup, Chargebee and Google
publish sitemaps that contain no individual job URLs; Nykaa runs a third-party
ATS (Skima) whose API path could not be guessed.

Each would need a browser-spoofed UA or a headless browser, which is out of scope.

## HTML adapters (Media.net, Skima / Nykaa)

`adapters/html.py`, for careers pages with no job API at all. The failure to
design against is not a crash but a selector that quietly matches nothing, which
reads exactly like a quiet week. So each adapter returns, besides its rows:

* `declared`: the count the page states itself ("Showing 5 of 5 - Jobs", a
  department's "4 Positions"). Parsed ≠ declared means the parse is partial: the
  rows are kept, nothing is retired, and the poll shows as failed.
* `empty_evidence`: the page's own words when it says there are no openings
  ("Currently there are no job postings available"). Zero rows **with** it is a
  genuinely empty board (a warning, but a valid sweep); zero rows **without** it
  means the selector broke (a failure, nothing retired).

and the runner (`_html_checks`) logs the parsed count on every run, warns on any
zero, counts consecutive zero-row polls per company (`zero_runs`) and after 3
logs `HTML adapter <company> returned 0 rows for N runs — verify manually`,
saying which kind of zero it is. A drop below half the previous count is called
out even when the page agrees. Structural anchors (Media.net's `#openings`
section, Skima's job search box) raise `AdapterError`: a layout change fails the
poll outright instead of reading as zero.

* **Media.net** (`careers.media.net`, WordPress, no robots.txt): the home page
  lists departments with counts; only departments declaring openings are
  fetched. Openings are `<ul class="openings-list"><li><a>`. Location is only on
  the job page (`<b>Location:</b> New York`), fetched for new rows; some pages
  state none, and those count as location-unknown matches like any vague row.
* **Skima** (`careers.nykaa.com`, Astro SSR, robots `Allow: /`): a card per job,
  title linking to `/<uuid>`, then spans for location, workplace, type. Built
  against `mccafe.skima.ai`, the same template with jobs, because Nykaa had none
  when this was written. Pagination is buttons with `data-last-page`; `?page=N`
  is tried and trusted only if the page answers `data-current-page="N"`.

## Detection notes

Company careers pages are fetched with the same honest `User-Agent`. Several
refuse it — `careers.nutanix.com`, `blinkit.com/careers`, `jobs.natwestgroup.com`
and `www.rubrik.com` answer 403 or a Cloudflare challenge. That is recorded, not
worked around; the underlying board is usually still reachable (Rubrik's
Greenhouse board answers fine even though its careers page does not).

Slug guessing is the weakest signal and is always flagged:

* `api.lever.co/v0/postings/eternal` → 200, but it is a US sports company, not
  Zomato/Eternal.
* `careers.smartrecruiters.com/Zomato1` → a real Zomato board, last updated
  **2016**.
* SmartRecruiters `uber` → one posting, "Test UAT", **2021**. Adobe, Sprinklr,
  Meesho, Glean, Graviton and Media.net all have similar abandoned
  SmartRecruiters boards that a naive probe would happily accept.
* Greenhouse `linkedin` → "LI Test Company"; Lever `linkedin` → "Anirban jobReq
  3 - public". Both are sandboxes.
* Ashby `navi` → 9 live, current postings for a **San Francisco startup**, not
  Navi Technologies of Bangalore. The freshest-looking false positive found so
  far: nothing about the response says it is the wrong company except the
  locations.

Hence `detect --write` refuses a guessed board unless `--accept-guess` is passed,
and prints sample rows plus the board's self-reported name first.

## Verified company resolutions

See `config.yaml` for the live config; each entry carries the date it was
verified. Companies resolving to an ATS with no adapter (iCIMS, Eightfold,
Avature/Radancy, SuccessFactors, Rippling's own ATS, self-hosted boards) are
recorded as `ats: unsupported` with a note and skipped at runtime.

## Verifying the job URL, not just the feed

A feed can be perfectly correct while every link it produces is dead. Quadeye
shipped that way: the rows were right, and `url` was built from the wrong
identifier, so all 26 links 404'd.

Rules that came out of it:

1. **Prefer a URL the payload gives you.** `absolute_url`, `hostedUrl`,
   `jobUrl`, `url_comeet_hosted_page` — if the publisher hands you a permalink,
   use it. Most adapters here do, and none of those broke.
2. **If you must build one, build it from the identifier the route uses.** That
   is not always the requisition number a human sees. Quadeye's route keys on
   the Zoho record `id` (`199893000000440465`), not `Job_Opening_ID`
   (`ZR_18_JOB`), and the two look equally plausible in the payload.
3. **Never synthesise a URL shape you have not opened.** Guessing a permalink is
   the same failure as guessing a board slug, which `detect` already refuses.
4. **On an SPA, the status code lies.** quadeye.com returns HTTP 200 for every
   path and draws "PAGE NOT FOUND" client-side. `curl -o /dev/null -w '%{http_code}'`
   says 200 for a link that is completely dead. Render the page and look for the
   title before believing it.
5. **A 404 on a closed req is normal.** Amazon and DocuSign both 404 postings
   that have left the feed. That is correct behaviour, not a bug — the export
   only publishes currently-open reqs, so those links never reach the phone.
   Check a job that is *currently in the feed* before concluding anything.

Audited 2026-09-18: one live URL per adapter, all 19 types. Every 404 traced to
a closed requisition except Quadeye's, which was the real bug.

Note that a fix here cannot reach the database on its own: boards that answer
**304 re-derive nothing**, so the corrected code never runs against the stored
rows. Follow an adapter change with `watcher seed --company <name> --refetch`.
