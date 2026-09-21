# Company-source recovery and Microsoft throttling audit — 2026-09-19

## Current verification — 21 September, 14:53 IST

Quest Global's scheduled 13:37 failure proved the earlier 900-row threshold was
not the complete fix: the 501-row Energy partition itself returned only 476
unique IDs under unstable relevance pagination. Phenom partitions now use
bounded replay with persisted per-pass IDs and still fail closed unless the
declared unique inventory is recovered. A live adapter run and the 14:36
production poll both completed at **1,001/1,001**; production has a null cursor
and error. The next normal scheduler cycle updated live KV at 14:49 to **166
companies / 2,764 matching jobs**. Site code was unchanged.

NTT DATA exposed the same threshold edge after its live count fell to 988: the
unpartitioned result returned 988 rows but only 922 unique IDs. Its existing
category + department facets currently form an exact 961 + 27 cover with a
largest bucket of 673, so `max_window: 900` now forces them below 1,000 too. A
full patched live fetch completed **988/988 in 46 requests / 112.7s**; the next
stored production poll remains to be observed.

Siemens's repeated restarts were narrowed to a concrete avoidable case. Its
explicit oldest-first result count grew from 944 to 946; the adapter used to
discard the entire hours-long cursor on any growth. It now extends on increases,
while retaining strict overlap detection and restarting on decreases. This is
unit-tested but not yet production-proven; the 14:29 old-code run had already
dropped the cursor.

Citadel Securities has completed normal 84-row polls at 02:12 and 11:00.
Microsoft remains unresolved: count drift restarted its offset-120 cursor at
00:32, followed by 429s at offset 0 (13:03) and offset 10 (14:12); progress is
retained at 10/235 through the 15:12 cooldown. **376 Python + 11 site tests
pass; `git diff --check` is clean.**

## Intake investigation — CoinDCX, 20 September, 21:56 IST

CoinDCX is not production-ready and was not configured. Its official
`careers.coindcx.com/opportunities` page and even that host's `robots.txt`
return HTTP 403 to the watcher's ordinary client. Publicly indexed job-detail
URLs expose encoded internal requisition IDs but not a complete, current
inventory or a verifiable request endpoint. No browser connection was available
for a non-challenge page inspection. No challenge was solved, no indexed result
was imported, and no ATS tenant was guessed. Recheck the exact official page at
low frequency; if it becomes readable, trace its frontend request and require
employer identity, declared/returned/unique coverage and explicit locations.

## Latest intake — 20 September, 21:54 IST

Arm is enabled through its official TalentBrew India country search after a
silent 105-row production baseline. The source declared/returned 105 unique
jobs across seven pages (98 Karnataka, seven Uttar Pradesh); 17 pass current
filters. Its sitemap was deliberately rejected for India completeness because
it had only 104 Bengaluru/Noida URLs and omitted a real Cambridge/Sophia
Antipolis/Bengaluru multi-location posting. The dedicated counted-HTML adapter
validates employer/facet identity, counts, pages, IDs and next links, and
restarts on drift or overlap. Baseline notifications and export were disabled,
with zero unsuppressed rows. Config is **158 tracked / 150 active / eight
inactive**; **368 Python + 11 site tests pass** and the diff check is clean.

## Latest intake — 20 September, 21:47 IST

DP World is enabled through the exact Oracle Recruiting Cloud tenant linked by
its official careers page. The `location=India` API query declared and returned
**118 unique jobs in one request**, all with an explicit India primary or
secondary location. A public detail identifies the candidate site as DP World.
The lock-protected silent production baseline recorded 118 known/open and two
current filter matches, with no cursor/error/backoff, zero unsuppressed rows,
no notification and no export. No new adapter was required. Config is **157
tracked / 149 active / eight inactive**.

## Latest intake — 20 September, 21:44 IST

IBM's already-completed 730-row silent baseline was verified and its config
flag was enabled without rerunning the baseline. MathWorks was then traced from
its official careers page to the published, robots-permitted full-board XML
feed. Its 193 entries collapse to 183 unique requisitions because ten entries
are exact duplicates; the 29 India entries collapse to **27 unique jobs**,
exactly matching the official India search's independent 27-result count.

A dedicated adapter validates feed identity, explicit country/location fields,
numeric IDs, first-party URLs and duplicate consistency. Its lock-protected
silent production baseline completed with 27 known/open, 12 current filter
matches, no cursor/error/backoff, zero unsuppressed rows, no notification and
no export. MathWorks and IBM are enabled. Config is now **156 tracked / 148
active / eight inactive**; **363 Python + 11 site tests pass** and
`git diff --check` is clean. Nothing is committed.

## Latest intake — 20 September, 21:23 IST

Cisco is enabled after a silent 302-row production baseline, the fourth
completed addition from the intake queue. Its official Phenom country search
reshuffled equal-count pages and was rejected for complete-sweep use. A new
adapter instead derives every live India office facet from Cisco's published
Workday location tree, persists that exact set across resumes, and requires
302/302 exact coverage. There are 130 current filter matches; no notification
or export occurred. Config is **154 tracked / 146 active / eight inactive**.
Exact source, failure-mode and baseline evidence is in the current
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) section.

## Latest intake — 20 September, 21:15 IST

A.P. Moller–Maersk is enabled after a silent 251-row production baseline, the
third completed addition from the new intake queue. Maersk's own careers UI
publishes a country-filtered API and read-only client key; its India query
returned 251 declared/returned/unique rows across 26 fixed ten-row pages, with
15 current filter matches. A dedicated fail-closed adapter and six tests cover
page/count coherence and publisher, country, Workday-site and URL identity.
The baseline sent no notification and performed no export. Config is **153
tracked / 145 active / eight inactive**. Exact source and production evidence
is in the current [SESSION_HANDOFF.md](SESSION_HANDOFF.md) section.

## Latest intake — 20 September, 21:00 IST

Nasdaq is enabled after a silent 30-row production baseline, the second
completed addition from the new intake queue. Its official Workday board's
published India country facet returned 30/30 unique rows across two pages;
seven pass current filters. The baseline sent no notification and performed no
export. Config is **152 tracked / 144 active / eight inactive**. Exact source,
facet and production evidence is in the current
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) section.

## Latest intake — 20 September, 20:53 IST

Synopsys, the first completed item from the new 27-company queue, is enabled
after a silent 500-row production baseline. Its robots-permitted official
TalentBrew sitemap has 500 unique job URLs; 155 India-city URLs exactly match
the official India-search count, and three pass current filters. No notification
or export occurred. The concurrently completed `nykaa-instahyre` recovery adds
24 silently baselined jobs (six matches), bringing config to **151 tracked /
143 active / eight inactive**. See the current
[handoff](SESSION_HANDOFF.md) for evidence and limitations.

## Latest recovery — 20 September, 20:38 IST

**149 tracked / 141 active / eight inactive, with zero employers wholly
uncovered.** The eight inactive entries preserve the first-party block records;
all eight now also have clearly named `-instahyre` secondary-source entries.
The last two, Gupshup and Krutrim, are known Instahyre employers returning an
authoritative zero rather than unknown-name errors. Both completed silent
production baselines at zero and are enabled every four hours. The earlier six
secondary entries remain enabled with 81 jobs between them at validation time.

Because a zero-row result exposes no numeric employer id, the adapter now
allows that config field to be absent. Future nonempty rows still require exact
employer-name equality, a positive numeric id, and one consistent id across all
pages and resumed runs. **335 Python + 11 site tests pass**; the two-company
baseline sent no notification and performed no export. See the newest
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) section for exact production evidence
and remaining work.

## Latest recovery — 20 September, 16:53 IST

**141 tracked / 133 active / eight inactive.** McKinsey and Citadel Securities
are now enabled every four hours after explicit user approval and complete
silent production baselines under RunLock (595 and 84 jobs, respectively).
Each currently has one role matching the title/location filters. All 679
backlog rows were suppressed, with no notifications or baseline export.
`watcher status` confirms both healthy, next due around 20:49 IST. Normal
scheduled polls and the newly updated live site snapshot remain unverified.

The previous “one automatable, nine cannot” assessment was too strong. Citadel's
official public form POST provides a complete counted inventory despite some
HTML routes remaining intermittently blocked. Gupshup's correct public POST and
Blinkit's HTML list are also now readable, but explicitly empty; both remain
inactive pending validation of a positive-job parser. Other remaining sources
retain their documented constraints, not a claim of permanent impossibility.

Two new adapters and nine safety tests; **313 Python + 11 site tests pass**.
No manual publication/deployment or commits. See the newest sections of
[BLOCKED_SOURCE_REVIEW.md](BLOCKED_SOURCE_REVIEW.md) and
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) for exact evidence, limits and state.

## Current verification — 20 September, 15:31 IST

**141 tracked, 131 active, 10 inactive.** LinkedIn is now enabled through
permitted SmartRecruiters career-page HTML, after a complete silent 57-job
baseline. This does not scrape the LinkedIn Jobs platform or the disallowed
SmartRecruiters postings API. Its 22 matching jobs are present in live KV.
Normal scheduled LinkedIn polls completed at 13:26 and 14:36 with 57 jobs and
no error/backoff. Uber also completed normal polls at 13:25 and 14:35 with 20.

Read-only Cloudflare verification found a newer scheduler-published snapshot,
generated_at=1789898297: **131 companies, 2,253 matching jobs, LinkedIn 22,
Siemens 82, 18 empty companies, no inactive companies**. The interrupted
publication no longer needs retrying. No deployment was performed here.

Microsoft's normal priority resume advanced to 90 at 13:25, then to 110/226
before a 429 at 14:35. The exact offset is retained, with cooldown until
15:35:29. A complete new sweep remains unverified. Siemens's normal sweep is
still partial (385 validated IDs, five pending details, offset 390/939).

Oracle's own tenant has repeatedly failed closed at 46/47; its last complete
inventory remains 46. Investigation is pending its next due request. Qualcomm
and Quest Global also have stale completed inventories; Quest Global's latest
error reports 962 unique jobs against an expected 1,000. These are unresolved,
not evidence of complete inventories or zero openings.

All **304 Python and 11 JavaScript tests pass**; `git diff --check` is clean.
Earlier sections are historical; the latest handoff remains authoritative.

## Latest outcome — 20 September, 11:31 IST

**Siemens completed its silent production baseline and is now enabled.** The
existing bounded process finished at 11:28:56 with 939 unique jobs, cursor null,
last_complete set, no error/backoff, and zero notifications/export. Independent
database checks validated every title/location/identity/URL and confirmed all
backlog notifications suppressed. Eighty-two jobs pass the current filters;
the three known non-India/global-location facet anomalies remain excluded.

The published Cloudflare snapshot was read back and verified: **130 active
companies, 2,236 matching jobs, 82 Siemens matches, 18 empty companies, no inactive
companies**, generated_at=1789884004. Only job data changed; no asset deployment.
There are 141 tracked companies and 11 inactive. No Siemens diagnostic remains.

Microsoft remains at 60/226 with no error/backoff and cannot resume before
12:11:22 IST. A normal priority resume and eventual coherent full sweep remain
unverified. Uber's first normal poll after its baseline is also pending.
All 294 Python and 11 JS tests pass; diff check clean. Nothing committed.
See the newest [handoff](SESSION_HANDOFF.md) for exact evidence and next steps.

## Latest pointer — 20 September, 11:18 IST

The newest [SESSION_HANDOFF.md](SESSION_HANDOFF.md) section supersedes older
counts/status below: **Uber is recovered and enabled after a complete silent
20-job Oracle baseline; 129 active companies have been published.** Microsoft's
cursor is 60/226 without backoff, and a tested scheduling change gives its due
partial sweep the next existing Eightfold slot without skipping cooldowns or
adding slots. A new complete Microsoft sweep remains unverified.

**A bounded Siemens baseline is still running** at this timestamp (PID 47327;
verify command before acting), at 432 validated IDs of 939. Siemens remains
disabled. See the handoff for process/log paths and exact safe completion steps.
Latest tests: 294 Python and 11 JavaScript pass; diff check clean. No commits.

## 20 September continuation — supersedes older current-state claims

Read the latest section of [SESSION_HANDOFF.md](SESSION_HANDOFF.md) first.
The live Pages snapshot now includes all **128 active companies**, including
18 with zero matching roles; disabled companies' stored rows are excluded.
The scheduler deployed the code at 22:07 IST on 19 September. Cloudflare API
verification confirmed the production deployment and 128-entry KV index;
successful deployed asset digest matches current files. Local phone-browser
checks pass; the protected live UI was not checked in a signed-in browser.

Microsoft's due-respecting silent check at 21:51 on 19 September and normal
10:11 poll on 20 September each fetched three pages without 429. The former
sweep restarted at 00:25; current cursor is 30/226, without backoff/error.
The last completed inventory is still 239. Full recovery is **not established**:
long shared-ATS rotation and mutable pagination remain. Added distinct logs for
count drift versus overlapping IDs without relaxing completeness validation.

Siemens now requests and validates ascending posted-date sort, persisted in
its cursor. An intentionally interrupted silent baseline retained 102 IDs,
offset 108/pending 6/total 953; it remains disabled and never completed.
Uber has a new anonymous Oracle CX_1 candidate source returning 20 India jobs,
not yet attributed/coverage-validated/baselined. This is an outstanding recovery
lead, not proof that Uber cannot be unblocked. Citadel sitemap was subsequently
read successfully; modern Python avoids Zepto's old TLS failure but still gets
403. Older reports of stopping on robots 403 describe the initial check only.

Final verification: **283 Python tests, 11 JavaScript tests, 26 local browser
checks passed; git diff --check clean**. No inactive source enabled, no commits,
no diagnostic notifications, no scheduler termination. See handoff for exact
state, evidence paths, next due time, and remaining tasks.

## Latest inactive-source review

See [BLOCKED_SOURCE_REVIEW.md](BLOCKED_SOURCE_REVIEW.md) for the subsequent
direct review of all twelve inactive companies other than policy-excluded
LinkedIn. No new production-ready recovery was established and no company
activation setting changed. This does not prove recovery impossible: Siemens
has a readable adapter but still needs a coherent production baseline.

This review qualifies older observations below: Zepto TalentRecruit's TLS
failure recurred on robots; Citadel Securities robots returned 403 and dependent
requests were stopped; Blinkit's inspected response was a branded Access Denied
403, without proof of a Cloudflare challenge. The newly examined Siemens jobs
widget exposes 21 detail links but no full inventory evidence. Exact Gupshup v2
config/inventory/filter requests still return Cloudflare 403. Full route evidence,
next triggers, and distinctions between old and fresh observations are in the
linked review.

This document records source validation separately from production enablement.
Counts are observations at validation time, not fixed expectations. A source is
only called enabled after a complete, silent production baseline; a working
sample or unit test is not a baseline.

## Current outcome

The configuration tracks **141 companies: 128 active and 13 inactive**. Twelve
companies recovered during this work are enabled and have completed silent
production baselines. No backlog notifications were sent during those baselines.

The live site's 110-company count is not a stale-export bug: the current export
contains 2,169 open rows passing both title and location filters, grouped into
exactly 110 companies, while its separate `watched` field correctly says 128.
The other 18 active companies currently have no matching open rows.

| Company | Verified source and scope | Completed baseline |
| --- | --- | ---: |
| Akamai | Oracle Recruiting Cloud, site `CX_1`, India query | 19 |
| Caterpillar | Workday `cat/wd5/CaterpillarCareers`, India country facet | 97 |
| Nutanix | Jobvite token `nutanix`, India region | 38 |
| Navi | First-party `navi.com/api/careers/jobs` | 41 |
| Qdrant | Official About Us link to Ashby board `qdrant.tech`; secondary locations retained | 5 |
| Vespa | Robots-allowed BambooHR `/careers/list`; no inferred India eligibility | 9 |
| GreyOrange | Zwayam company 16090 at `careers.greyorange.com` | 88 |
| HSBC | Eightfold apply API at `portal.careers.hsbc.com`, domain `hsbc.com`, India | 167 |
| BookMyShow | Public Trakstar HTML board; detail identity and public RSS IDs agree | 13 |
| Chargebee | SuccessFactors search at `jobs.chargebee.com`; Chargebee organization verified | 1 |
| Bain | Official counted careers API with the normal frontend Referer | 326 (16 configured matches) |
| Meta | Public anonymous careers location/search operations, dynamically filtered to India offices | 24 (3 configured matches) |

Caterpillar's timestamps are not ordered, so it uses complete India sweeps.
Nutanix resolves the multi-location rows from public detail pages. Navi's
unrelated Ashby token is a different company. Qdrant's Remote India secondary
location counts, while Europe-only remote does not imply India eligibility.
BookMyShow's main marketing site returned 403 during validation, so identity was
cross-checked on job details and its public RSS. Chargebee's current public
board contains one Remote US job; the list count and sitemap agree.

Bain's default ten-row ordering changed across a page boundary while its count
remained 326, so the adapter requests the current inventory in one 400-row
response. It still validates count, identity, links, page size, overlap, and
resume state. Three source rows explicitly omit location; they remain in the
counted inventory but cannot qualify for the strict India/city filter.

Meta's global public search returned 989 unique jobs and one distinct featured
job, exactly matching all 990 IDs in its allowed job sitemap. Four global rows
had no location. Meta's live location-filter operation exposed five India
offices, and its search across those offices returned 24 unique jobs with no
empty location; the four global locationless IDs were absent. The adapter parses
fresh anonymous request context each run and fails closed if a persisted query
ID or response shape changes. The older JSON-LD sitemap prototype remains only
as historical evidence and is not the active adapter.

## Microsoft 429 diagnosis

Microsoft's India inventory uses the public Eightfold PCSX endpoint. The most
recent completed inventory contained 239 jobs, ten rows per request. The log
contains 13 timestamped Microsoft HTTP 429 warnings and three Qualcomm warnings;
no other inspected Eightfold tenant logged a 429. Two tenants failing near each
other makes a shared tenant/IP quota plausible, but the service has not exposed
or documented the quota in the inspected responses.

Observed Microsoft sequence in IST:

- September 18: offsets 0, 120, 200, and 210 throttled during several attempts;
  a sweep later completed at 235 jobs. Further attempts throttled at offsets 40,
  80, and 100.
- September 18 23:21: the old three-hour cursor expired. Requested timestamp
  ordering then proved unordered, so the incremental path restarted a full scan
  and immediately duplicated work before another throttle.
- September 19: later attempts throttled at offsets 90, 10, 30, and 90. The last
  offset-90 failure happened on the first request after about 136 minutes idle,
  showing that six-second spacing alone cannot prevent every 429.
- One diagnostic request after the recorded cooldown returned 200 with count
  239 and no rate-limit or `Retry-After` header. It did not mutate production.

Implemented safeguards:

- Microsoft no longer uses incremental stopping. It still requests stable
  timestamp sorting, but never trusts that order to stop early or performs a
  failed incremental probe followed by a refetch.
- Each run is limited to three pages and a partial cursor must wait at least one
  hour before resuming. The cursor lifetime is 24 hours.
- A throttle with no new rows preserves its backoff level. Consecutive
  no-progress throttles now wait at least one, two, then four hours. Real page
  progress resets escalation; a server `Retry-After` remains authoritative.
- Eightfold cursors now validate per-location counts, count drift, repeated
  pages, incomplete pages, and legacy cursor shape before completion can close
  jobs.
- A drift/overlap restart now clears the abandoned sweep's epoch as well as its
  cursor. Rows seen only before the restart can therefore close normally after
  the replacement sweep completes. Bain, Siemens, and Zwayam use the same
  explicit restart contract.

Nine Eightfold companies share a one-company-per-run scheduler lane. At a
15-minute launch cadence, one fair rotation can take about 2.25 hours. Microsoft
needs roughly 24 pages at the current count, so eight three-page chunks can take
about 18 hours before throttling, sleep, or count drift. The changes reduce
duplicate traffic and make failures back off correctly; they do not establish
the server's exact quota or guarantee an hourly completed inventory.

The normal scheduler exercised the legacy-cursor guard at 17:01 IST: it logged a
restart with zero fetched rows and cleared both `cursor` and `sweep_started`
without making another Microsoft request. All backoff/error fields are clear.
No fresh replacement chunk had run by 21:21; the shared Eightfold rotation and
run budget continued to defer Microsoft, so post-reset three-page progress and
the 1h → 2h → 4h no-progress throttle escalation remain unverified in production.

## Validated but not production-ready

### Siemens

The permitted server-rendered search supports an India country facet and returns
six rows per page. Its displayed total changed from 955 to 957 to 954 during
validation. The adapter checks displayed ranges, next-page links, duplicate IDs,
detail identity, count drift, and resumable pending detail work. A count change
or overlap restarts without declaring closures.

The diagnostic validation completed coherently at 954 unique postings with no
empty title/location or identity mismatch. It resolved 120 multi-location rows;
951 rows passed the strict India/city filter and 82 also matched configured title
terms. The country facet is search membership, not proof that every detail is in
India: job 516009 listed only Toronto/Troy, 517911 Detroit/Mexico City, and
493275 "Any Siemens location in the world." Those rows remain in the counted
inventory but do not pass the India filter.

The production baseline is a separate milestone. Five bounded attempts detected
live count/page drift, returned the explicit restart signal with zero rows, and
recorded no baseline. The repeated loop was stopped to avoid unnecessary traffic.
Siemens remains configured but disabled until one bounded production sweep can
complete coherently.

A full recheck found no cheaper completeness source. The robots-advertised
locale sitemap contains 58 static portal URLs and zero jobs. The permitted
India-search RSS is capped at 20 items, lacks locations/count, and is not proven
newest-first. A later live search declared 953 results, reinforcing that the
counted HTML sweep must tolerate a moving board without claiming a false
snapshot; the RSS cannot safely retire closures or replace the baseline.

## Tracked but blocked or unresolved

| Company | Current evidence |
| --- | --- |
| LinkedIn | Excluded by project policy. |
| Ola | Both official Mobility and Electric pages expose no inventory and link the same TurboHire organization. Its `User-agent: *` rule disallows `/`; no board path was fetched. This does not establish Krutrim coverage. |
| Uber | Allowed sitemap currently has 531 unique IDs/lastmods but no title/location. Honest details and filtered listings return Cloudflare challenges. |
| Gupshup | Official opportunities CTAs use WhatsApp. Both Darwinbox candidate apps load and reveal current v2 inventory routes, but every company-config/all-jobs/filter request returns Cloudflare 403. |
| Krutrim | The new official AI Labs Open Positions button leads only to LinkedIn, which project policy excludes. The old careers route remains 404. |
| Flipkart | The real official TurboHire board remains robots-disallowed. A permitted first-party `/jobslist` is only a static ten-card placeholder with truncated fields, no IDs/links, and a fake Show More alert. |
| Zepto | Main careers path returns an AWS WAF challenge. The officially linked TalentRecruit board and its robots URL consistently return a plain AWS ELB 403. |
| Zomato | Eternal's live careers bundle says applications are referral-only and makes no listing API request. Its old SmartRecruiters tenant has only three abandoned 2015–2016 rows. |
| Blinkit | The robots-permitted official careers listing returns Cloudflare 403. An indexed zero-result rendering cannot be used as a pollable baseline; `jobs.blinkit.com` identifies as a Vendor Dashboard, not a verified careers board. |
| Instabase | Official JS still hard-codes dead Greenhouse board `instabase`; board/API and all three exposed detail IDs return 404. Cards contain placeholder copy and the sitemap has no jobs. |
| Citadel Securities | Career sitemap has 88 detail slugs but no IDs/title/location. Details return Cloudflare challenges and some slugs are evergreen, so current India inventory cannot be established. |
| McKinsey | Search-index copies show current roles, but direct search/detail/sitemap requests time out with zero bytes. The accessible Avature host is only a candidate/application portal. |

## Safety and verification boundaries

All enabled sources use the watcher's honest HTTP client and obey robots rules.
No bot challenge, proxy, browser impersonation, application, subscription, or
outbound message was used. Partial scans retain cursors and cannot close unseen
jobs. Counted HTML sources require count/pagination evidence before an empty or
complete result is trusted.

Follow-up review also moved counted Trakstar/SuccessFactors count drift and page
overlap onto the explicit restart contract, and made Meta validate, filter, and
deduplicate global featured rows instead of silently dropping them. Regression
coverage was added for both cases.

Verification after integration: **280 Python tests** and **11 JavaScript tests**
pass, and `git diff --check` is clean. Bain and Meta have completed silent
production baselines. Siemens completed its diagnostic validation but not a
production baseline and therefore remains disabled.
