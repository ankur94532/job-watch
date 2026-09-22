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

Nasdaq was added on 2026-09-20 from the Workday tenant linked by its official
careers page (`nasdaq.wd1`, `Global_External_Site`). The board's published
`Location_Country` facet returned exactly 30 declared/returned/unique India
jobs across two pages. Its silent production baseline completed before
activation; seven rows pass the configured filters.

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
  `paloaltonetworks`; **no** on `walmart` (1 day, 30 days, 1 day)
  and `nvidia` (3, 3, 2, 3). `salesforce` passed that sample and failed the
  live per-page check on its first incremental poll (Today after Yesterday at
  offset 20), so it stays on full sweeps: a sample is not proof. On a newest-first tenant, `incremental: true` reads
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

Cisco is a stricter version of that pattern. Its current official Phenom site
publishes `cisco.wd5/Cisco_Careers` apply URLs and a country facet near 300
India jobs, but consecutive 100-row Phenom pages reshuffled and produced only
286–300 unique IDs for the same declared 300. The dedicated `cisco` adapter
therefore reads Workday's live location facet first, selects every descriptor
ending `, India` plus the legacy `IND-*` location, persists that exact ID set in
the resume cursor, then runs an exact-count Workday sweep. This returned 302 of
302 unique postings across ten current India location IDs on 2026-09-20 and
will automatically include a newly published India office on the next sweep.

## Maersk official careers API

Maersk's own permitted vacancies page calls
`GET https://api.maersk.com/careers/vacancies?country=India&offset=N...` using
a read-only `Consumer-Key` published in its client JavaScript. The API host has
no robots policy (its `/robots.txt` answers 404), while the public careers page
is allowed. This is safer than selecting dozens of individual India office IDs
from the underlying Workday tenant: the publisher's country filter will include
new offices automatically.

The endpoint declared 251 India jobs on 2026-09-20 and returned exactly 251
unique rows over pages 0–25. `offset` is a **page number**, not a row offset;
pages are fixed at ten rows and ignore a larger `limit`. Every live row carried
`Brand: Maersk`, `Platform: Workday`, `Country: India`, a unique Workday URL on
the `Maersk_Careers` or `APMT_Careers` site, and a matching `ExternalId`. Ten rows omitted `City`, so the adapter retains their
authoritative `India` location rather than inventing a city. It holds every page
against `ResultCount` and `ResultsPerPage`, and restarts on count drift or page
overlap instead of trusting a potentially incoherent sweep.

## IBM first-party careers search

IBM's own `ibm.com/careers/search` component calls the public, robots-permitted
`www-api.ibm.com/search/api/v1/ibmcom/appid/careers` service with scope
`careers2`. `filter=field_keyword_05:India` returned **730 declared, returned
and unique** numeric job IDs on 2026-09-20; every row echoed the exact filter,
country, nonempty location, date and a unique `careers.ibm.com` job URL. The API
accepts `nr=1000`, so this is one coherent response rather than shifting pages.

`rmdt=ALL` was 9.5 MB because every row carried roughly 170 mostly empty
attributes. Requesting only `field_keyword_05,field_keyword_19,
field_keyword_17,dcdate` retained all watcher fields and reduced the complete
response to about 661 KB. The adapter checks the echoed app, collection, source,
empty query and India filter plus declared/returned counts and every row's
sequence, country, metadata and URL identity before accepting the sweep.

## MathWorks first-party XML feed

MathWorks' official careers search publishes a “job feed” link to the
robots-permitted `https://www.mathworks.com/company/jobs/opportunities/rss.xml`.
The careers HTML itself returned 403 to the watcher's ordinary client, but the
machine-readable XML feed is directly readable without cookies, credentials or
browser impersonation.

On 2026-09-20 the feed contained 193 entries and 183 unique requisition IDs.
Ten items were exact duplicates, including two India jobs. Filtering the
feed's explicit `<location><country>IN</country>` fields produced 29 entries
and **27 unique India requisitions**, exactly matching the official India
search's independent 27-result count. India locations were 19 Bangalore, nine
Hyderabad and one Delhi before duplicate collapse; 12 unique jobs passed the
watcher's current title/location filters.

The adapter groups repeated IDs, collapses exact repeated ID/location pairs,
and rejects conflicting titles or URLs. Every item must have a numeric ID,
title, two-letter country, nonempty location and an HTTPS MathWorks URL whose
path repeats the same ID. Six valid current URLs omit the optional title slug,
so both `/opportunities/123-title.html` and `/opportunities/123.html` are
accepted. A missing/malformed feed or zero target-country rows fails closed.
The source publishes no reliable opening date, so `posted_at` is intentionally
blank. Its silent production baseline completed at 21:44 IST with 27 jobs, 12
matches, no cursor/error/backoff, zero unsuppressed rows, no notification and
no export.

## Eightfold and Microsoft's specialized wrapper

The pcsx API accepts `sort_by=timestamp` (not `sortBy` or
`sort_by=postedTs`) and echoes `sortBy: "timestamp"`. That produced stable
newest-first pages in a 2026-09-18 Morgan Stanley check, but it is a request,
not a guarantee across tenants. An incremental board re-checks timestamps on
every page and falls back to a full sweep if they increase. Microsoft failed
that check repeatedly on 2026-09-18 and 2026-09-19, so it does not use
timestamp order to stop at known IDs. Its specialized adapter instead reads
the three newest India roles from Microsoft's server-rendered official page
every 15 minutes. It still requests timestamp order for closure sweeps to keep
resumed pages as stable as the service permits. The apply/v2 flavour
(Netflix, NetApp) has also returned unordered rows and is never incremental.

Ordinary Eightfold adapters use `max_parallel = 1` and `max_per_run = 1`. Microsoft and
Qualcomm were both observed returning 429s close together, so the conservative
scheduler rotates one Eightfold tenant per 15-minute process. That timing does
not establish a shared quota, a per-IP quota, or the service's quota window;
the inspected responses disclosed none of those details. Microsoft's latest
page does not consume that slot. Its closure API is independently limited to
two pages and an hourly cursor-local resume cadence.


`GET https://{host}/api/pcsx/search?domain={domain}&query=&location={location}&start=N&num=10`

Multi-tenant, but the host is per company (`apply.careers.microsoft.com`) and the
tenant is named by `domain`. Results are nested under `data`:
`data.count` (total for the query, present on every page) and `data.positions[]`
with `id`, `displayJobId` (the requisition number a referral needs), `name`,
`locations[]`, `standardizedLocations[]`, `postedTs` (epoch seconds),
`workLocationOption`, `positionUrl`.

* **`num` is capped at 10** — asking for 50 or 100 still returns 10. Microsoft's
  last complete India inventory on 2026-09-19 was 239 positions, so a sweep was
  24 requests at that count.
* `start` past the end returns an empty list. No wrap-around.
* Pages advance by rows returned, not by `num`, so a short page cannot skip rows.
* `location` filters server-side, which is the point: unfiltered, Microsoft's
  board is thousands of rows at 10 a page. One query per configured location,
  deduped by position id.
* Microsoft returned 429s with 0.5s, 2s, and 6s page spacing. On 2026-09-19 it
  also throttled the first request at offset 90 after about 136 minutes without
  a Microsoft request. A single diagnostic request after the stored cooldown
  returned 200, but exposed neither `Retry-After` nor a rate-limit header. Page
  spacing remains polite and may affect burst behavior; it does not by itself
  prevent the observed 429s. The exact limit and whether tenants share it are
  unknown.
* **Microsoft full sweeps span many hourly slices.** From 21 to 22 September
  the closure sweep used the official `filter_profession=software engineering`
  facet (235 -> ~89 roles). That hid title-matching roles in other professions
  (for example "Technology Consulting") and retired every stored role outside
  the facet, so on 22 September it moved to every India role with anchored
  resume (`resync_budget: 2`, `resync_slices: 12`). `postedTs` is the latest
  re-post time, so re-posts jump to the front; `profession` remains an option. Each closure attempt is capped at two
  pages, keeps a 24-hour cursor, and waits at least one hour after a partial
  chunk. Consecutive 403/429 responses with no progress delay API retries by
  1h, 2h, 4h, and so on, without delaying the independent latest-page poll; a
  server `Retry-After`, when present, remains authoritative.
* The latest page and closure API originally shared one runner cursor cadence:
  while a closure cursor existed, `resume_interval: 3600` suppressed the
  claimed 15-minute path, and Microsoft's inherited Eightfold group cap could
  defer it behind another tenant. The specialized adapter now has its own
  scheduler group, remains due every 15 minutes, and stores `api_after` plus an
  API-only backoff level inside its closure cursor. Latest-role reads therefore
  continue during each hourly wait without increasing API traffic.
* **Anchored resume (`anchor_resume: true`: Qualcomm and Microsoft).** Qualcomm's
  ~600-row India inventory needs ~60 pages at a quota of roughly 15 requests
  per window, and it gained 23 postings in ~45 minutes on 2026-09-22, so the
  offset guards restarted every sweep. An anchored sweep still accepts a page
  by offset only when the count is unchanged and no ID repeats. Otherwise it
  re-syncs: it finds a page whose first row is strictly newer than the oldest
  row read, continues from that timestamp boundary, and restarts if an unseen
  row older than the sweep's first row appears behind the boundary, if rows
  are out of timestamp order, or if it cannot find the boundary within six
  requests. It then makes a second pass, with the same drift handling, from
  the top down to the first pass's newest row, so a job re-posted ahead of
  the first pass's position is not falsely closed, and it requires the final
  count to be no larger than the IDs read. A resumed slice always starts with
  a boundary-checking page, since the list has usually moved in the gap. Qualcomm returns a batch
  of postings that share one timestamp in a different order on every request,
  so a same-timestamp group that spans pages is read by count: its first and
  last positions are witnessed by strictly newer and older rows, its IDs are
  collected until they equal that size (re-reading its pages up to three
  times), and the board's count must not change meanwhile. An unfinished
  re-sync is saved as its own phase, so a paused slice can never resume by
  plain offset, and it restarts after four slices without success. Residual risk: an old job
  re-listed behind the boundary at the same time as enough closures to hide
  it from the final count check would be missed until the next sweep.
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

## Oracle Recruiting Cloud (JPMorgan Chase, Uber)

DP World's official careers page links directly to
`ehpv.fa.em2.oraclecloud.com` / `CX_1`. Its exact `location=India` query on
2026-09-20 declared and returned **118 unique jobs in one request**; every row
explicitly named India in its primary or secondary locations, and a public job
detail identified the candidate site as DP World. The silent production
baseline stored all 118 with two current filter matches, no cursor/error/
backoff, zero unsuppressed rows, no notification and no export before the entry
was enabled.

Uber recovered on 2026-09-20 through `iaziqy.fa.ocs.oraclecloud.com` / `CX_1`.
The anonymous candidate page identifies itself as Uber Careers; its 531 global
IDs exactly matched the official `jobs.uber.com/en/jobs/sitemap.xml`. The India
query's 20 jobs exactly matched the global inventory's primary/secondary India
locations. Two public detail responses confirmed IDs, titles, locations, and
Uber descriptions. A silent complete production baseline recorded all 20 with
no cursor/error/backoff before enabling; three pass the current local filters.
Oracle-generated job URLs use the same branded, accessible candidate site.

The adapter accepts `secondaryLocations[].Name` as well as the older field
names. Counts, unique IDs, page coverage, required identity/title/location, and
resume state are validated; drift/overlap restarts without declaring closures.
403/429 retains successful rows and the precise resume offset. Legacy cursors
restart without HTTP. These safeguards also apply to existing Oracle tenants.

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

## Radancy / TalentBrew via sitemap (Intuit, Synopsys)

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
Synopsys was independently validated on 2026-09-20: its permitted official
sitemap contained 500 unique job URLs, and the 155 URLs in its six India city
slugs exactly matched the official India-search count. Three pass the current
title/location filters. It completed a silent 500-row production baseline
before activation. Intuit's sitemap remains a partial source; completeness is
tenant-specific. `careers.walgreens.com` and `jobs.cvshealth.com` publish
nothing comparable, so this does not automatically generalise to TalentBrew.

Arm is also a TalentBrew site, but its sitemap is **not** sufficient for India.
On 2026-09-20 the official India search declared and returned 105 unique jobs
over seven server-rendered 15-row pages (98 Karnataka, seven Uttar Pradesh).
The simultaneously generated sitemap held only 104 Bengaluru/Noida URLs. The
missing posting was real: its canonical URL uses Cambridge because that is the
primary office, while its public detail lists Cambridge, Sophia Antipolis and
Bengaluru. City-slug inference would therefore silently lose a valid India job.

The dedicated `arm` adapter reads the exact country-filtered pages instead. It
validates the Arm organization ID, India facet ID, declared count, page number,
page size, total pages, card IDs and same-host published next links. Count drift
or cross-page overlap restarts the sweep rather than closing rows from a mixed
snapshot. Exact country-facet membership is retained as `India` when a
multi-location card exposes only `Multiple locations` or a foreign primary
office. The silent production baseline completed at 21:54 IST with 105 jobs,
17 current filter matches, no cursor/error/backoff, zero unsuppressed rows, no
notification and no export.

## Meta public careers search

The anonymous careers page publishes fresh LSD/SiteData request context. The
adapter uses that context with the public location-filter and job-search Relay
operations, an ordinary `Origin`/`Referer`, and the repository User-Agent. It
does not hardcode transient page tokens or require cookies.

On 2026-09-19, global search returned 989 unique `all_jobs` plus one distinct
featured job; those 990 IDs exactly matched the allowed job sitemap. Four global
jobs had empty locations. The live location-filter operation exposed Bangalore,
Gurgaon, Hyderabad, Mumbai, and New Delhi for India, and searching those offices
returned 24 unique jobs with no empty location. None of the four global
locationless jobs appeared in the India result. Persisted operation IDs can
change when Meta deploys, so query errors or an unexpected response shape fail
the poll without closing stored jobs.

## Siemens counted India search

The public India-faceted HTML board returns six rows per page. The adapter
requests `postedDate:ASC` and validates the server's sort echo, result count,
page ranges, next links, unique IDs, and detail identity. Multi-location rows
are resolved from details before first storage; later sweeps reuse that stored
location for known IDs and fetch details only for new ones. Pending detail work
persists across bounded chunks. Count drift or page overlap restarts without
declaring closures. A restart can retain only a fresh offset-zero/no-ID cursor,
so the replacement sweep gets a new epoch and a bounded 30-minute retry.

The silent production baseline completed on 2026-09-20 at 11:28 IST with
939 unique jobs, no cursor/error/backoff, and all backlog alerts suppressed.
All rows have validated identity/title/location/URLs; 82 pass configured filters.
Three non-India/global-location facet results remain in inventory but fail the
strict India filter. Siemens is enabled. Its hard six-row pagination now has an
opt-in 900-second poll budget after earning a shared scheduler slot; this does
not extend the 120-second deadline for starting other paginated boards. The
21 September 21:35 attempt proved that isolation but restarted safely on a live
956 -> 955 count decrease at offset 426, so a new complete sweep is still open.

## SmartRecruiters career HTML (LinkedIn as an employer)

`careers.smartrecruiters.com/LinkedIn3?search=India` publishes counted location
groups. The career page's own `/LinkedIn3/api/more` route supplies HTML for its
Show more control; this is separate from the robots-disallowed REST API at
`api.smartrecruiters.com`, which was not fetched. No LinkedIn-platform scraping,
login, browser impersonation, or challenge solving is involved.

On 2026-09-20, inspection of all six global group pages established the complete
India location set: Bengaluru (51), Gurgaon (2), Mumbai (4). Country search
returned those same groups, and its seven-request sweep produced 57 unique
jobs. Detail samples in every city confirmed employer, title, location, public
application URL and September 2026 posting dates. The silent production baseline
completed at 11:47 IST: 57 stored, 22 configured matches, no cursor/error/backoff,
all notifications suppressed. LinkedIn is now enabled through this adapter.

The adapter checks organization, complete group pagination, visible/attribute
counts, initial nine-card limits and subsequent ten-card pages, safe links,
unique IDs, and a final reread of starting counts and group heads. Rows stay in
the resumable cursor until verification completes. Missing cards fail, overlap
or changed inventory restarts, and a throttle retains the exact pending page.
Empty inventories require the explicit empty-state text on two reads.

## Checked and still unresolved

This list was revalidated on 2026-09-19. It excludes sources recovered since
the older investigation: Nutanix, Chargebee, BookMyShow, Qdrant, Vespa, Navi,
Akamai, Caterpillar, HSBC, GreyOrange, Bain, Meta, Uber, Siemens, and LinkedIn
(through SmartRecruiters) are now polled. Google, Apple,
Myntra, Hasura, D. E. Shaw, PhonePe, ScyllaDB, and Quadeye also have working
adapters elsewhere in this file. See `AUTOMATION_AUDIT.md` for source and
production-baseline evidence.

| Company | Current evidence |
| --- | --- |
| Gupshup | Both Darwinbox candidate apps expose current inventory route names, but company config/all-jobs/filter requests return Cloudflare 403. Official CTAs use WhatsApp. |
| Krutrim | Official AI Labs now has an Open Positions button, but it leads only to LinkedIn, excluded by project policy. |
| Flipkart | Official TurboHire board disallows this crawler. The permitted first-party jobs list is an incomplete static placeholder without IDs or links. |
| Zepto | Main careers path returns an AWS WAF challenge; the officially linked TalentRecruit board consistently returns AWS ELB 403. |
| Zomato | Eternal's live careers bundle is referral-only and makes no job API call; the old SmartRecruiters board is abandoned. |
| Blinkit | Allowed official listing returns Cloudflare 403. Indexed zero-result text is not a pollable baseline; `jobs.blinkit.com` is a Vendor Dashboard. |
| Instabase | Official page still names dead Greenhouse board `instabase`; board/API/details return 404 and page cards are placeholders. |
| Citadel Securities | Sitemap has 88 detail slugs but no IDs/title/location; direct details are Cloudflare-challenged and some entries are evergreen. |
| McKinsey | Indexed copies show roles, but direct search/detail/sitemap requests time out; the accessible Avature site is only a candidate portal. |
| Ola | Official Mobility and Electric pages expose no inventory and link one TurboHire organization whose robots rules disallow the watcher. |

These restrictions are left visible rather than bypassed with challenge solving,
proxying, user-agent impersonation, or guessed third-party board identities.

## Goldman Sachs Higher

`POST https://api-higher.gs.com/gateway/api/v1/graphql`, operation `GetRoles`,
no login: `roleSearch(searchQueryInput: {page: {pageSize, pageNumber}, sort,
filters: [], experiences: [EARLY_CAREER, PROFESSIONAL], searchTerm: ""})`
returns `totalCount` and roles. `pageSize` 200 works, 500 is refused; the
adapter uses 100. robots.txt on `higher.gs.com` allows only `/roles/` (the
listing, `/results`, is disallowed); `api-higher.gs.com` has no robots.txt.

**Job links need a browser to verify.** The page is client-rendered and answers
200 for any id: `/roles/<externalSource.sourceId>` shows the role, while
`/roles/<roleId>` ("184745_GS_MID_CAREER") and a made-up id both render "Oops,
something went wrong". Relevance order, so pages are deduplicated and the
count is held against `totalCount` (below 95% raises rather than retiring).

## Kula (Slice) and job sitemaps (Wise)

* **Kula** (`careers.kula.ai/<account>`): the careers page fetches
  `/api/internal/ats_job_posts`, but Kula's robots.txt says `Disallow: /api/`.
  The listing page is allowed and server-renders every job, so `KulaAdapter`
  (in `html.py`, with the HTML-adapter safeguards) reads that. Its CSS classes
  are generated hashes, so cards are read structurally; `declared` is the count
  of distinct job links, an independent selector. Job URLs have no trailing
  slash: `/slice/50594/` answers 308.
* **Sitemaps**: `SitemapAdapter` is the Radancy reader with the URL shape from
  config (`job_url`, named groups `slug`, `city`, `req`). Wise publishes
  `vacanciessitemap.xml` (named in its robots.txt, which disallows the paged
  `/jobs?...` listing); `/job/<title>-in-<city>-jid-<id>` splits on the LAST
  `-in-`, so "specialist-in-payments-...-in-sao-paulo" keeps its title.

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
# Recovery candidates added 20 September 2026

`mckinsey` and `citadel_securities` are registered, tested and enabled with
four-hour intervals after explicit user approval. Both completed live silent
production baselines under RunLock at 16:50–16:51 IST: 595 and 84 jobs,
respectively. All backlog notifications were suppressed; export was disabled.

- McKinsey: anonymous first-party counted JSON, one request for up to 800
  global postings. Enforces total/unique identity, Avature folder identity and
  aligned city/country arrays. Preserves all locations. No posting date inferred
  from LinkedIn syndication metadata. The deployment-specific gateway path may
  rotate; public detail pages remain unverified from the local HTTP client.
- Citadel Securities: official `careers_listing_filter` form POST at
  `/wp-admin/admin-ajax.php`, one counted response of up to 100 HTML cards.
  Current four role-type IDs (`323,325,324,326`) match the official form's default
  selection. Ten-second delay after robots fetch. Public slugs are posting IDs,
  never requisition numbers; dates are omitted. Some listings are evergreen
  expressions of interest. Access remains intermittent on other page paths.

Both fail closed on truncation, duplicates, unexpected shape or growth beyond
the verified single-response bound. Neither can treat a partial sweep as a
complete inventory. See `BLOCKED_SOURCE_REVIEW.md` for evidence and limitations.
