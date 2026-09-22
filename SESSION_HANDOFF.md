# Job-watch session handoff — 2026-09-20

## START HERE — 22 September, 14:30 IST — Microsoft moves to every India role

**State: 414 Python + 11 site tests pass; `git diff --check` clean. All work
through this section is committed and pushed to `main` (the commit after
`b23c8cb`), at the user's request, before Qualcomm's production proof.**

### Why

The Software Engineering facet had a side effect nobody had flagged: when its
closure sweep completed at 05:25, every stored Microsoft role outside the facet
was marked closed. Of the roles seen in the 3 days before, 18 match the title
filters and are no longer shown as open. Some are genuinely closed, but several
sit in other professions ("Technology Consulting": ".Net Full Stack & AI"
consultants; "Solution Engineering - Cloud Infrastructure"). A cover by
profession is not practical: the filters' broad words (full stack,
infrastructure, platform, machine learning) span professions, the page names
only some profession values, and counting each costs as much as a full sweep.

### Evidence gathered (3 requests total)

- careers.microsoft.com India page and the apply.careers.microsoft.com careers
  shell (robots allows `/careers` and `/api/pcsx`): profession values include
  Consulting Services, Data Center, Digital Sales and Solutions, Human Resources,
  Sales, Software Engineering, Supply Chain, Technical Support, Technology Sales.
- One pcsx request (`sort_by=timestamp`, India): 229 roles, `sortBy`
  "timestamp", rows strictly non-increasing in `postedTs` (not `creationTs`).
  `postedTs` is the latest re-post time. Rows carry `department` (the
  profession): "Software Engineer II - GitHub" is Software Engineering;
  "Sr Consultant - Infra AI" is Technology Consulting. Old ordering failures
  (18-19 Sep) would now restart the sweep, not corrupt it.

### What changed

- `config.yaml` microsoft: `profession` removed, `anchor_resume: true`,
  `resync_budget: 2`, `resync_slices: 12`. `MicrosoftAdapter.profession` is now
  optional.
- Anchored sweeps, reworked while testing against Microsoft's 2-request slices:
  - The completion head check is now a **second pass** with the same drift
    handling, down to pass 1's newest row. The old single-slice head check could
    not fit Microsoft's quota and could skip a re-posted row when resumed by
    offset. Residual: a job re-posted twice, both times ahead of the cursor.
  - A resumed slice starts with a boundary-checking page (~17-18 new rows per
    2-request slice instead of ~7).
  - The anchored cursor carries the query scope (without it, every Microsoft
    resume would have restarted).
  - The re-sync request minimum and slice limit are per company.
- Tests: an unfiltered 229-row Microsoft sweep at 2 requests/hour, with a
  re-post, a new role and a closure every hour, completes in ~15 slices and
  reads every role that existed at sweep start (checked on six seeds). A
  posting rate far above the quota restarts rather than guessing. Mutation
  checks: disabling re-sync fails 8 tests; the second pass 6; the group read
  2; re-sync phase persistence 1; the cursor scope 2.

### Next

1. Qualcomm: first run on this code after its 14:14 backoff (~14:28).
2. Microsoft: its next closure sweep starts when `full_sweep` (24h) elapses,
   about 23:26 tonight, and should finish ~13-15 hourly slices later
   (tomorrow afternoon, longer with 429s). Until then non-SWE roles remain
   shown as closed. New roles in every profession keep arriving through the
   15-minute official latest page.
3. If Qualcomm or Microsoft logs an `anchored sweep ... restarting` line, its
   reason names the guard that fired; investigate before changing any guard.
4. NTT DATA's Phenom restart path is still unexercised in production.

## START HERE — 22 September, 14:15 IST — Anchored resume fixed for same-timestamp groups

**State: 411 Python + 11 site tests pass; `git diff --check` clean. Still
uncommitted (see 13:10). `main` = `origin/main` = `b23c8cb`.**

The 13:09 slice worked (0 -> 200 on the new code). The 13:26 slice re-synced
seven times with no progress. Cause: Qualcomm returns a batch of postings that
share one `postedTs` in a different order on every request, and that group was
larger than a page, so no page could witness both of its edges. This probably
also explains the 12:41 old-code overlap at offset 10 (the 23-posting batch).

Fixed and tested:

- A same-timestamp group spanning pages is read **by count** (see
  `read_group` in `eightfold.py`). If the first pass already has all members,
  one re-read of the start edge confirms it (about 7 requests for 23 rows).
- **Real bug found while testing:** when a re-sync paused on budget, the cursor
  saved the new count with the old offset, and the next slice's fast path could
  accept a stale offset and skip a row. A re-sync is now its own cursor phase
  (`phase: "resync"`) and can only resume as a re-sync. Production was checked:
  every acceptance so far was a strict page or a completed re-sync, and the
  13:27 pause was followed by a re-sync rather than a fast-path accept, so the
  cursor (start 197) was kept.
- A re-sync may use at least 12 requests (Qualcomm's cap is already 20), and it
  restarts after 4 slices without success, so it cannot loop forever.
- `resyncs` now counts consecutive re-syncs without progress (reset on each
  strict page).
- Three new tests (unstable 23-row group with drift, a rate limit mid-re-sync,
  and a group member that never appears). Mutation checks: disabling re-sync
  fails 6 tests; disabling the group read fails 2; not persisting the re-sync
  phase fails the rate-limit test.

**Next:** Qualcomm's backoff ends at 14:14. Expect `re-syncing`, then possibly
`read a N-row same-timestamp group by count`, and eventually `qualcomm: N open`.
Any `anchored sweep ... restarting` line names its reason.

## START HERE — 22 September, 13:10 IST — Qualcomm switched to anchored resume

**State: 177 tracked / 166 active / 11 blocked; 408 Python + 11 site tests
pass; `git diff --check` clean. `main` = `origin/main` = `b23c8cb`. The change
below is uncommitted (`watcher/adapters/eightfold.py`, `config.yaml`, the
adapters README, the new `tests/test_eightfold_anchored.py`, and this handoff).**

### Why

The 15-minute priority resume worked mechanically (11:22 0 -> 200, 11:39
200 -> 350, then 429 until 12:09), but at 12:24 the count had jumped 592 -> 615
and at 12:41 an overlap at offset 10 restarted again. Qualcomm posts faster
than a quota-bounded ~60-page offset sweep can finish, so the offset guards
could never let it complete. The user chose the anchored-resume option over
narrowing to one category or accepting no closure retirement.

### What changed

Opt-in `anchor_resume: true` (Qualcomm only; single location, pcsx with
timestamp order). Normal pages are accepted by offset under the same strict
conditions as before. On a count change or repeated ID, the sweep re-syncs on
the newest-first timestamp boundary instead of restarting, and at completion it
re-reads the newest pages back to the sweep's first row. It restarts on rows
out of timestamp order, an unseen *older* job behind the boundary, a boundary
not found in 6 requests, more than 12 re-syncs, or a final count above the IDs
read. Design and residual risk are in the "Anchored sweeps" comment in
`eightfold.py` and in the adapters README. Ten new tests, mutation-checked:
disabling re-sync fails the insertion, closure, tie and re-post tests;
disabling the head check fails the re-post test.

### Next

1. **Production proof.** Qualcomm held a zero-progress offset cursor from
   12:41, which the anchored path accepts as a fresh start (a non-zero offset
   cursor would have restarted). Expect log lines `Eightfold list moved ...;
   re-syncing` rather than `restarting`, then `qualcomm: N open` with a
   cursor-free state, within roughly 4-6 of its 15-minute slices, allowing for
   429 pauses. Any `anchored sweep ... restarting` line names the reason.
2. Commit and push once it is proven (or when the user asks).
3. NTT DATA's new Phenom restart path is still unexercised in production.

## START HERE — 22 September, 11:50 IST — Qualcomm moving; NTT DATA recovered

- **Qualcomm fix is working so far.** It got its Eightfold slot at 11:22
  (0 -> 200). The 15-minute priority resume ran at 11:39 (200 -> 350), then hit
  HTTP 429 at offset 350. The cursor (350 IDs) is retained, with backoff until
  12:09. Last complete sweep is still 2026-09-20 16:01. Next: confirm that it
  resumes from 350 after 12:09 and completes (~580) without an overlap restart.
  If 429s keep splitting the sweep, consider lowering `max_pages_per_run` so
  each slice stays under the quota. Do not relax guards.
- **NTT DATA recovered:** the stale cursor aged out at 10:39 and it completed
  at 11:21 (1,042). The new Phenom restart path has not been exercised in
  production yet. IDFC FIRST completed at 10:40 and 11:39 (1,236).
- Siemens restarted safely at 10:09 (overlap) and 11:06 (951 -> 950 at offset
  642) after its 09:01 completion. This is expected fail-closed behavior; its
  zero-progress cursor retries on the 30-minute cadence.
- Everything from 10:25 onward is still uncommitted.

## START HERE — 22 September, 10:25 IST — Phenom stale-cursor loop fixed; Qualcomm proof pending

**State: 177 tracked / 166 active / 11 blocked; 398 Python + 11 site tests
pass; `git diff --check` clean. HEAD is `ffbd213` on `job-sources-sep-2026`
(not merged, not pushed). Uncommitted: the Qualcomm change (see 10:05), the
Phenom fix below, their tests, and this handoff.**

### NTT DATA was stuck, not flaky — fixed

NTT DATA failed with the identical message `Phenom {'field': 'category',
'value': 'Other'} count changed during sweep (724 to 737)` in nine consecutive
runs from 07:43 to 10:02. `PhenomAdapter` raised `AdapterError` on a partition
count change, and an error keeps the saved cursor, so every retry compared the
live count (737) against the same stale stored count (724). Only the 3-hour
`CURSOR_MAX_AGE` (sweep started 07:26) could break the loop. IDFC FIRST uses
the same adapter and was exposed to the same trap.

A count change now returns `FetchResult(restart=True)` with a warning. The
runner starts a fresh sweep epoch and declares no closures, which is the same
contract as the Eightfold and Siemens restarts. Completeness rules are
unchanged. Regression test:
`test_count_drift_on_resume_restarts_instead_of_retrying_stale_cursor`
(verified to fail without the fix).

**Verify:** NTT DATA's next failure on a moving partition should log
`count changed during sweep ...; restarting` once, not repeat the same error,
and it should complete again within a few runs.

### Other live checks at 10:16

- Siemens completed 951 at 09:01, then at 10:09 correctly restarted on a page
  overlap at offset 666 (new behavior: zero-progress cursor, 30-minute retry).
  That is normal fail-closed behavior, not a regression.
- Walmart completed at 07:00 (1,973); Honeywell 09:28 (329); Target 1,980;
  Microsoft latest-page polls continue every 15 minutes.
- Qualcomm is still waiting its turn in the Eightfold queue (last polled 08:26),
  as described at 10:05.

### Remaining items

1. **Qualcomm production proof** (expected first slice around 10:45-11:15, then
   two 15-minute resumes to a cursor-free ~580).
2. **NTT DATA/IDFC FIRST:** confirm the Phenom restart behavior in production.
3. **Microsoft scope caveat:** non-Software-Engineering roles are not
   closure-inventoried (accepted limitation unless it matters).
4. **Git:** commit the Qualcomm and Phenom changes; merge into `main` and push
   only when the user asks.

## START HERE — 22 September, 10:05 IST — Qualcomm fix reviewed; waiting on its first slice

**State: 177 tracked / 166 active / 11 blocked; 397 Python + 11 site tests
pass; `git diff --check` clean. HEAD is `ffbd213` on `job-sources-sep-2026`
(not merged, not pushed). The Qualcomm change below is uncommitted: `config.yaml`,
`watcher/adapters/eightfold.py`, four test files and this handoff.**

### Review of the 08:56 Qualcomm change

- Production has **not** exercised it yet, and that is expected. Qualcomm's
  08:26 overlap restart ran on the old code, so it has no cursor. Its
  `resume_priority` only applies to an *active* cursor, so the first slice waits
  in the normal oldest-first Eightfold rotation (`max_per_run: 1`, eight
  tenants, ~2h). It became due at 09:26:34, about 30s after the 09:26 run gave
  its Eightfold slot to HSBC. Netflix, NetApp and Morgan Stanley have older
  `last_polled` values, so expect the first slice around 10:45-11:15. The
  "deferred (out of time)" lines at 09:29 and 09:46 report the per-run
  Eightfold cap, not an actual time shortage.
- The generic `fresh_cursor()` on Eightfold drift/overlap applies to **every**
  Eightfold tenant, not just Qualcomm. That is safe (the runner still starts a
  fresh sweep epoch), and tenants without `resume_priority` stay behind in the
  one-slot rotation. Checked Microsoft specifically: its restart cursor still
  gets `api_after = now + 1h` through the general deferral path, so a drift
  restart cannot hit the quota-limited API early. Added
  `test_count_drift_restart_still_waits_before_the_next_api_call` to pin that.

### Remaining items

1. **Qualcomm production proof.** After the first slice (offset 200), expect
   resumes at 400 and then a cursor-free ~580 completion on consecutive
   15-minute runs. A drift restart must leave a zero-progress cursor and retry
   within about 15 minutes; a 429 must keep progress and obey backoff. If
   front-insertion drift still restarts it inside a ~30-minute window, the next
   option is a narrower official facet (as for Microsoft); do not relax the
   overlap guard.
2. **Walmart:** resolved. 2,000 is the API wrap boundary, and the 09:01 run
   completed at 1,973. Keep the 95% coverage rule.
3. **NTT DATA:** the moving `Other` partition still fails occasionally but
   completes regularly. Watch only.
4. **Microsoft scope caveat:** roles outside Software Engineering are not
   closure-inventoried.
5. **Git:** commit the Qualcomm change once it is proven (or when the user
   asks). Merge into `main` and push only when the user asks.

## START HERE — 22 September, 08:56 IST — Qualcomm drift fix implemented; production proof pending

**State: 177 tracked / 166 active / 11 blocked; 396 Python + 11 site tests
pass; `git diff --check` clean. The five Qualcomm-related files below plus this
handoff are uncommitted on `job-sources-sep-2026`. Do not commit, merge or push
unless the user asks.**

### Qualcomm diagnosis and fix

- The cached Eightfold client and Qualcomm's current official careers shell
  confirm `sort_by` is server-driven and the tenant exposes the standard sort
  controls, but no tenant-defined oldest-first order. Do not guess an
  undocumented reverse-sort value.
- A single read-only diagnostic request to the official Qualcomm index at 08:52
  received HTTP 429 before returning a body. No retry was made. This is evidence
  against increasing the existing 20-page per-run cap or giving Qualcomm an
  independent concurrent Eightfold lane.
- The safe fix keeps the count and overlap guards unchanged. Qualcomm now has
  `resume_priority: true` and `resume_interval: 900`, so only an active closure
  cursor takes the next shared Eightfold slot every 15 minutes. A normal
  ~58-page sweep should therefore take three closely spaced slices (~30
  minutes) instead of three slices spread over 2-3 hours each. Backoff is still
  checked first, and peers keep every slot when Qualcomm has no cursor.
- Eightfold count-drift and page-overlap restarts now return a validated empty
  cursor (`loc/start=0`, no ids, no total). The runner already starts a fresh
  sweep epoch on `restart`, so no rows from the incoherent attempt survive; the
  cursor exists solely to keep the replacement attempt on the 15-minute resume
  cadence. Tests cover count drift, page overlap and apply-flavour drift.

### Verification and next work

- `make test`: **396 Python + 11 site tests pass**. `git diff --check` is clean.
- Production proof is still required. Qualcomm had no cursor after its 08:26
  overlap restart and is next normally due at 09:25. Let launchd earn the normal
  Eightfold slot; do not force another request after the diagnostic 429. Once it
  starts, expect offsets 200 then 400 on consecutive eligible 15-minute runs,
  then a cursor-free completion around 580. A drift restart should leave a
  zero-progress cursor and retry 15 minutes later; a 429 should retain progress
  and obey backoff.
- **Walmart's 2,000 question is resolved without a code change.** At 08:57 the
  official Workday endpoint returned the exact same 20 `externalPath` values at
  offsets 0 and 2,000 while reporting `total: 2000` both times: 2,000 is the
  API wrap boundary, not an exact unique-count promise. The 09:01 normal run
  then completed cursor-free with 1,973 unique rows, inside the existing 95%
  coverage rule. Recent 1,7xx-1,8xx runs were correctly rejected below 95%; do
  not weaken that threshold.

## START HERE — 22 September, 08:45 IST — Microsoft proven; Qualcomm is the remaining drift problem

**State: 177 tracked / 166 active / 11 blocked; 395 Python + 11 site tests
pass; `git diff --check` clean. All Siemens, Honeywell and Microsoft work is now
committed on `job-sources-sep-2026` (the commit after `e745dda`). The branch is
still not merged into `main` and not pushed; do neither without the user's
instruction.**

### Resolved overnight (normal scheduler, no manual runs)

- **Microsoft is proven.** The filtered closure sweep completed cursor-free at
  05:25: **87 open**, error/backoff null (`last_complete` shows 23:26 because it
  records the sweep epoch start). Every 15-minute run since has read exactly the
  three newest official-page roles. The only remaining caveat is scope: roles
  outside the Software Engineering profession are not closure-inventoried.
- Siemens completed again at 08:00 (**951**), Honeywell at 08:27 (**329**),
  Target is cursor-free at 1,980, NTT DATA completed at 05:59 (1,042).

### Still open

1. **Qualcomm (last complete 2026-09-20 16:01, ~40h STARVING).** Not a quota
   problem any more — it is drift. Its ~58-page sweep is split into 20-page
   chunks, but it only gets an Eightfold slot every ~2-3h, and its
   `sort_by=timestamp` order is newest-first, so new postings shift earlier
   pages and the overlap guard restarts it: offset 70 (22:33), 30 (02:57) and
   200 (08:26, after pausing at 200 at 05:43). The guard is correct; the
   schedule cannot currently produce a coherent sweep. Options to evaluate:
   an oldest-first sort if the pcsx API honors it (lets growth append, as with
   Siemens), a shorter `resume_interval` with its own slot, or a narrower
   official facet as for Microsoft. Do not relax the overlap guard.
2. **Walmart (last complete 2026-09-21 16:48, 1,949).** Failed closed four more
   times today (1,708 x2, 1,866, 1,885 of a reported 2,000). The failure rate
   is now high enough to investigate whether the 2,000 figure is a capped total
   rather than true inventory; until then keep the fail-closed rule.
3. NTT DATA's moving `Other` partition still fails intermittently (latest:
   count changed 724 -> 737 mid-sweep) but completes regularly; watch only.
4. **Microsoft scope caveat:** roles outside the Software Engineering
   profession are not closure-inventoried. Revisit only if non-SWE Microsoft
   roles become relevant.
5. **Git:** merge `job-sources-sep-2026` into `main` and push — only when the
   user asks.
6. Re-run `make test` and `git diff --check` after any edit, and add a new
   START HERE section with production evidence.

## START HERE — 22 September, 00:23 IST — Microsoft cadences separated; closure advanced

**State: 177 tracked / 166 active / 11 blocked. All work remains uncommitted on
`job-sources-sep-2026`; preserve the 14 modified and two untracked files. Do not
commit, merge or push without the user's instruction.** The full suite passes
at **395 Python + 11 site tests**, and `git diff --check` is clean. Re-run it
after any further edit.

### Microsoft scheduling defect found and fixed

The 23:50 design did not actually provide a 15-minute latest-role path during a
closure sweep. Any cursor made runner scheduling use `resume_interval: 3600`,
and the specialized adapter still reported `ats = eightfold`, so its cheap
official-page read also competed for the one Eightfold tenant slot. The 23:52
normal run demonstrated both symptoms: Morgan Stanley took the slot and
Microsoft was not polled.

The two cadences are now independent:

- Microsoft has its own scheduler group and remains due every 15 minutes.
- Its cursor carries `api_after` for the quota-sensitive closure API. Successful
  two-page chunks wait one hour; zero-progress 403/429s delay only the API by
  1h, 2h, 4h ... up to 24h. The latest page is not globally backed off.
- An incremental latest-page result deliberately preserves the existing closure
  cursor and sweep epoch; this has runner regression coverage.
- Status now displays a cursor's real `resume_interval` due time instead of
  claiming every unfinished sweep is due `now`.

Normal production proof is complete for the cadence split:

- At 00:19 Microsoft read two API pages, advanced its scoped closure cursor
  **0 -> 20**, stored `api_after` = 01:20:38, and recorded 20 rows / three new
  without a throttle or global backoff. HSBC completed 157 rows in the same run.
- At 00:37 the next normal launch read exactly the **three newest official-page
  roles** (zero new), while the database preserved offset 20, all 20 IDs, facet
  total 87, `api_after` 01:20:38, and null error/backoff. Qualcomm used the
  separate Eightfold slot in that same run.

### Other live evidence and open work

- Siemens completed another normal cursor-free **955/955** sweep at 00:02 in
  the 23:52 run. Its private budget remains isolated and proven.
- Honeywell completed normally again at 00:20 with 327 rows; its two tolerated
  one-row partition shortfalls were logged explicitly.
- Qualcomm finally got the 00:37 Eightfold slot, advanced to offset 30, then
  received HTTP 429 and correctly retained its cursor with backoff until 01:07.
  Its last complete sweep remains 20 September 16:01, so it is still STARVING;
  do not force it during the recorded cooldown.
- Target completed its long sweep at 00:38 with **1,980/2,000** unique rows,
  inside Workday's existing bounded coverage rule; its cursor/error are null and
  the STARVING condition is cleared. Walmart failed closed again at 1,708/2,000;
  do not add tolerance. NTT DATA's prior strict moving-partition failure remains
  unchanged.
- Microsoft still needs a cursor-free filtered closure count (roughly 89) before
  it is fixed. Also retain the caveat that roles outside the Software
  Engineering profession are not closure-inventoried.

## START HERE — 21 September, 23:50 IST — Siemens and Honeywell proven; Microsoft rescoped

**State: 177 tracked / 166 active / 11 blocked. 389 Python + 11 site tests
pass; `git diff --check` is clean.** The four handoff commits remain on
`job-sources-sep-2026` at `e745dda`. Everything below is **uncommitted**: 13
modified files plus the new `watcher/adapters/microsoft.py` and
`tests/test_microsoft.py`. Do not merge or commit without the user's instruction.

### Resolved since 21:47 (production evidence)

- **Siemens is proven.** The 22:50 normal run completed a cursor-free
  **955/955** sweep at 23:02 (run 690.7s) with 36 new rows and two alerts. The
  private 900s `poll_budget` did not widen the shared window: Wells Fargo,
  Target and Walmart paused at the 120s boundary while Siemens continued.
- **Honeywell is proven.** Oracle caps pages at 200 and its offset pagination
  repeated a boundary ID even with a stable total. `OracleAdapter` now covers
  the India inventory with the official disjoint 3 workplace x 2 posting-date
  facets (every partition < 200 rows, one request each). Two partitions stably
  advertise one inaccessible row each, so `count_tolerance: 1` applies **per
  partition only**. Live validation: 327 unique in 10 requests; normal
  production poll at 23:18: **326 open**, cursor/error null.
- **Eightfold backlog mostly cleared:** Morgan Stanley (21:58, 101), Eightfold
  AI (22:50), Lam Research (23:17, 116), UKG (23:27, 75), Netflix (23:28, 9).
  IDFC FIRST (1,225) and Quest Global (1,021) completed again at 23:17.

### Microsoft — rescoped, closure sweep not yet proven

Root cause is a host quota (roughly one-two requests before 429), not paging.
The full India query needs ~24 pages, so it could never finish. New design
(`ats: microsoft` -> `MicrosoftAdapter`, still scheduled as an Eightfold tenant):

- **New roles:** every 15 minutes, one request to the robots-permitted official
  India page (`careers.microsoft.com/.../l-india`), which lists the 3 newest
  roles with ID/title/location/date. Layout drift fails closed (exactly 3 valid
  cards required). It has already produced a 3-new alert.
- **Closures:** a daily sweep using the official `filter_profession=software
  engineering` facet (**89 India roles vs 235**), 2 pages per run, hourly
  resume, `resume_priority: false` so it no longer starves healthy tenants.
- The cursor now carries a `scope`; the old unfiltered cursor was discarded via
  a restart, and production holds a fresh scoped offset-0 cursor (23:26).

Caveats to confirm: (a) Microsoft roles outside the "Software Engineering"
profession are no longer inventoried — Codex reported checking that stored
title-matching jobs fall inside the facet, but that evidence is not recorded
here; (b) more than 3 India postings within 15 minutes would be missed by the
fast path until the daily sweep sees them (only if they are in the facet).

### Still open

1. **Microsoft closure sweep:** needs ~5 hourly two-page runs without a 429.
   Require a cursor-free complete filtered count (~89) before calling it fixed.
   Last complete sweep is still 2026-09-20 10:11 (37h STARVING warning expected
   until then).
2. **Qualcomm** (last complete 2026-09-20 16:01, 31.6h) and **Target** (last
   complete 2026-09-20 14:35; cursor at 1,520/2,000 and advancing) remain
   STARVING. Qualcomm is still being deferred as out-of-time; watch whether it
   gets an Eightfold slot now that Microsoft has no priority.
3. **Fail-closed recurrences at 23:36 (correct behavior, watch only):** Walmart
   `got 1827 of 2000` (23rd such failure since 09-19; last complete 16:48,
   1,949) and NTT DATA's moving `Other` partition (651/658; last complete 21:00,
   988). Do not add tolerance to either without new evidence.
4. **Git:** commit this work on `job-sources-sep-2026`, then merge/push to
   `main` — both only when the user asks.
5. Re-run `make test` and `git diff --check` after any edit.

## START HERE — 21 September, 21:47 IST — Siemens gets a targeted coherent-sweep budget

**State: 177 tracked / 166 active / 11 blocked. 382 Python + 11 site tests
pass; `git diff --check` is clean.** The four handoff commits remain on
`job-sources-sep-2026` at `e745dda`; the Siemens throughput work below is
**uncommitted**. Preserve the modified config, runner, adapter, tests and the
updated documentation. Do not merge or commit without the user's instruction.

### Production facts that changed after the 16:55 handoff

- NTT DATA is now proven in normal production: complete polls at 15:57 (978),
  17:24 (967) and 21:19 (988), with null cursor/error after completion.
- IDFC FIRST's strict Phenom guard eventually completed normally at 21:00 with
  **1,226/1,226** open and 14 new rows. Earlier failures on the moving `Others`
  partition were still correct fail-closed results; no tolerance or replay
  relaxation was added.
- Citadel Securities completed another normal 84/84 poll at 20:59. Quest Global
  completed normally at 16:49 (1,006) and 20:59 (1,015).
- Microsoft remains unresolved. Its 20:59 request was throttled at offset 0 and
  backed off until 21:59. Do not increase its page cap.
- Qualcomm and several other large tenants were temporarily poll-starved after
  the laptop's multi-hour sleep/catch-up backlog. The 21:35 run did recover
  NetApp and several large Workday boards; do not call the backlog fixed until
  later normal rotations clear the starvation warnings.

### Siemens throughput change — implemented and tested, completion still open

The shared 120-second run deadline made Siemens's six-row pages span ~4.5 hours,
long enough that ordinary closures repeatedly forced the safe count-decrease
restart. Raising the global budget would also make every large Workday/Phenom
poll longer, so the runner now supports an opt-in per-company `poll_budget`:

- The shared deadline still decides whether another paginated company may
  start. Siemens alone gets up to 900 seconds after it has earned a slot.
- Siemens can make up to 320 requests in that slot, at 0.8-second configured
  pacing. Other companies retain the existing limits.
- Known multi-location Siemens IDs no longer refetch detail pages. Every such ID
  was resolved before entering the store, and `Store.record` deliberately
  preserves an existing row's location; only new IDs still require detail
  resolution before filtering/alerting.
- A count/overlap restart may now carry only a validated zero-progress cursor.
  The runner starts a fresh sweep epoch and Siemens waits 30 minutes before the
  retry, instead of keeping incoherent IDs or waiting the full normal interval.
  Other adapters that return no restart cursor retain the old clear-and-wait
  behavior. Seed runs remain deadline-free.

The first normal run under the private budget started at 21:35 from the existing
offset-72 cursor. It proved the scheduler isolation: many cheap boards completed
normally and three large Workday boards paused at the shared boundary while
Siemens continued. At 21:43 Siemens correctly restarted when the declared count
fell **956 -> 955 at offset 426**. The run finished normally in 482.6 seconds
and published the current 166-company snapshot. **This is not a Siemens
completion.** The known-detail optimization and zero-progress restart cursor
were added after that run and have unit coverage but no production proof yet.
Because the already-recorded restart used the old behavior, current production
state is cursor-null and next due around 22:34; do not rewrite the DB to hurry it.

### Next actions

1. Observe the next normal Siemens attempt. Require a cursor-free complete count
   before calling the throughput fix proven. A real decrease/overlap must still
   fail closed; after a future restart, confirm the stored cursor is exactly
   offset 0 with no IDs and the retry waits at least 30 minutes.
2. Observe Eightfold rotation after Microsoft's cooldown. Qualcomm,
   Morgan Stanley, Eightfold AI, Netflix, Lam Research and UKG were stale at
   21:43; normal oldest-first slots should clear them. Do not force requests or
   infer a shared quota from the warnings alone.
3. Re-run 382 Python tests, 11 site tests and `git diff --check` after any edit.
   Update this section with production evidence. Do not commit/merge unless the
   user asks.

## START HERE — 21 September, 14:53 IST — Phenom fixes proven; Siemens restart narrowed

**State: 177 tracked / 166 active / 11 blocked. 376 Python + 11 site tests
pass; `git diff --check` is clean. Nothing is committed** (HEAD remains
`ccc8057`); preserve all tracked and untracked work.

### Quest Global is now proven in production

The earlier `max_window: 900` change was necessary but not sufficient. Its
13:37 scheduled poll declared 1,001 jobs, read the expected row counts, but
collapsed to only 985 unique IDs. A traced live run identified the exact cause:
Phenom's relevance order reshuffles *inside the facet partitions too*. The
501-row Energy facet returned 501 rows but only 476 unique IDs; the redundant
unpartitioned sample returned 900 rows but only 886 unique.

`PhenomAdapter` now keeps per-pass and cumulative partition IDs and replays a
deficient real facet at most twice. It still requires the declared unique
inventory and fails closed after the bounded retries; replay state survives a
deadline/cursor. The request cap was raised from 50 to 64 so two permitted
replays can actually finish. Two regression tests cover recovery and exhausted
retries.

- Patched live fetch: **1,001/1,001 unique, 51 requests, 33.2s**.
- Production poll under RunLock at 14:36: **1,001 open, cursor/error null**;
  eight new rows, four current filter matches. The configured notification sink
  is stdout because Telegram token/chat are blank in both `jobswatch.env` and
  the installed launchd plist; those matches were recorded as delivered in the
  normal console-sink sense.
- The manual poll could not publish without the env file. A follow-up `make site`
  with `deploy/jobswatch.env` loaded updated live KV at 14:39; the next normal
  scheduler cycle superseded it at 14:49 with **166 companies, 2,764 matching
  jobs**. Site code was unchanged.

### Siemens no longer throws away progress for ordinary growth

The board still forces six rows per page, so one ~946-row sweep spans hours.
The old code restarted on *any* count change even though it explicitly requests
oldest-first order. Logs show restarts at 18:03, 22:35, 11:19, 12:46 and 14:29;
the last cursor declared 944 and a fresh first page declared 946, confirming
normal additions were enough to erase progress.

The adapter now extends an in-progress sweep when the count increases. This is
safe under the confirmed oldest-first order: newest additions append at the
end, while an insertion before the cursor shifts a previously seen ID onto the
next page and the existing overlap guard restarts. Count decreases still
restart because a closure before the cursor can create an undetectable gap.
The adapter now logs count decreases and overlaps separately. Unit coverage
proves growth continues 2 -> 3, while decreases and overlap still fail closed.

**Production validation remains open.** The 14:29 run used the old behavior and
dropped the cursor; the next scheduled Siemens attempt starts over under the
new logic. It still needs roughly 18 productive runs (~4.5h while the laptop is
awake) and can still restart on a real closure or overlap.

### Other handoff items rechecked

- **Citadel Securities is resolved:** after the 20:51 403 it completed normal
  84/84 polls at 02:12 and 11:00 with no new rows. It is no longer
  baseline-only.
- **Microsoft is still unresolved and the 429 was not a one-off.** The 23:23
  cursor later restarted at 00:32 because the declared count changed 226 -> 221
  at offset 120. It then waited behind the one-Eightfold-tenant-per-run rotation
  until 13:03, got a 429 on offset 0, accepted only offset 0 at 14:12, and got
  another 429 at offset 10. The cursor is safely retained at 10/235 with
  backoff until 15:12. Recent 15-minute tenant rotation has not freed enough
  Microsoft quota; do not increase its page cap. The next investigation should
  test a narrower first-party query only after cooldown, and must prove it still
  covers every configured title term before replacing the full India inventory.
- **NTT DATA's matching threshold bug is fixed and proven outside the store.**
  Its 14:47 scheduled attempt fell to 988 total, so the old nominal 1,000-row
  threshold stopped facet partitioning; 988 returned rows collapsed to only 922
  unique. `max_window: 900` now forces the already configured category +
  department cover even below 1,000. Fresh facets were exact and safe: 961
  categorized + 27 `department=Unspecified` = 988, largest bucket 673. A full
  patched live fetch completed **988/988 in 46 requests / 112.7s**. Its next
  normal production poll is the remaining proof; the last stored complete
  inventory is still 1,029 from 23:58.
- **Qualcomm** restarted at 13:37 on count drift 581 -> 582 at offset 200 and
  remains sweep-starved. Its last complete inventory is from 20 September 16:01.

No install/restart is needed for these Python changes: each launchd invocation
imports the working tree afresh. Do not commit without the user's instruction.

## START HERE — 21 September, 16:55 IST — work committed; open items below

**The work is now COMMITTED.** It previously existed only in the working tree.

Branch **`job-sources-sep-2026`**, branched from `main` at `ccc8057`, four
commits:

```
5fb92b3  Track 166 active companies; record why 11 stay blocked
9c88c53  Run on OpenSSL 3 and stop the phone caching a stale app shell
13c7d11  Add 20 job sources and harden completeness guards
```
plus the documentation commit that carries this file.

**It is NOT on `main` and NOT pushed.** To merge when you are happy:

```
git checkout main && git merge --ff-only job-sources-sep-2026
```

**State: 177 tracked / 166 active / 11 blocked. 376 Python + 11 site tests
pass.** Scheduler runs `/opt/homebrew/bin/python3` (3.14.5, OpenSSL 3.6.2).

### Remaining work

Ordered by what actually costs you something.

1. **Siemens has not completed a sweep in ~29h.** Restarts are fixed (see
   below); the throughput ceiling is not. Six rows per page is a hard server
   cap — tested, `folderRecordsPerPage` 25 and 50 both return exactly 6 — so
   ~950 rows costs ~157 requests at ~50 rows/run, bound by the shared 120s
   `run_budget`, not by `max_requests_per_run` (16, using ~9). Options: a
   larger budget (affects every paginated board), a dedicated slot, or accept
   that it retires closed roles slowly.
2. **IDFC FIRST fails closed on its Phenom `Others` partition** (372 unique of
   374). Safe — nothing is seeded — but closed roles are never retired.
   **Do not "fix" it by raising `partition_replays` or adding a
   `count_tolerance`**; both were measured and are wrong. Evidence in the
   16:40 section below.
3. **Citadel Securities has never completed a NORMAL scheduled poll.** Its only
   completion is the 16:50 baseline on 20 Sep; normal polls return HTTP 403
   from `wp-admin/admin-ajax.php`. The adapter already honours the site's 10s
   crawl-delay. Decide: intermittent WAF, or the POST route refusing repeat
   traffic. Do not solve a challenge; a longer interval is the honest lever.
4. **Watch the Eightfold 429s.** Microsoft took one at 23:23 on 20 Sep and
   Qualcomm one at 16:30 on 21 Sep, after hours of none following the caps. Two
   in a day is not yet a trend, but if they continue the per-tenant caps need
   revisiting. Cursors are kept, so no work is lost.
5. **Joveo** — access is solved, shape is not. Its board answers 200 with robots
   `Allow: /` and a 10-job sitemap, but it is a widget CMS: 20 MB home page,
   89 KB details exposing only widget/site/client IDs, no JobPosting JSON-LD
   and no clean location field. Location drives the India filter, so an adapter
   means parsing widget trees for 10 jobs. `joveo-instahyre` watches it instead.
6. **Nykaa shows 46+ consecutive zero-row polls.** Working as designed —
   `careers.nykaa.com` genuinely states there are no postings — and the real
   roles are in `nykaa-instahyre` (24, 6 matching). It will keep warning; that
   is the HTML adapter refusing to treat zero as normal.
7. **Cloudflare Access sign-in failed once and was never diagnosed.** It
   recovered on its own. This repo's API token returns 0 Access applications,
   so it cannot see the app. If it recurs: Zero Trust → Access → Applications →
   Policies, and check the Include rule lists the address being used. The
   Function verifies against `white-river-f80d.cloudflareaccess.com`, which
   matches the live redirect, so nothing here rejects the login.
8. **Always use the bare `job-watch-live.pages.dev`.** Each deploy also gets a
   permanent immutable address; a bookmark on one serves that old build and its
   frozen data forever. That is the likeliest cause of the "3 days old" report
   on 21 Sep, on both devices at once.

### Not started

- The intake queue is finished: **24 resolved, 3 dropped** (Flexiple, "scoutit",
  Google DeepMind). No queued companies remain.
- No further sources are pending investigation.

## Earlier — 21 September, 16:40 IST — IDFC Phenom gap investigated

Codex implemented two fixes at 14:29-14:53 today (Siemens count-growth
tolerance, Phenom partition replays). Both are real improvements. One left a
new failure, investigated below. **376 Python + 11 site tests pass.**

### Siemens — restart problem fixed, throughput problem NOT

`watcher/adapters/siemens.py` now extends a sweep when the count grows instead
of restarting, exploiting the `postedDate:ASC` ordering where new jobs append
rather than shifting earlier pages. Confirmed live in the log:

```
15:56:47 siemens: Siemens count grew 948 -> 949 at offset 54; extending sweep
16:13:41 siemens: Siemens count grew 949 -> 950 at offset 102; extending sweep
```

It also persists the current page alongside resolved IDs so a detail-fetch
throttle cannot lose work. Eight tests in `tests/test_siemens_followup.py`.

**But it still has not completed — 29.3h.** The restarts are fixed; the
throughput ceiling is untouched and is the real blocker: six rows per page is a
hard server cap (tested — `folderRecordsPerPage` 25 and 50 both return exactly
6), so ~950 rows costs ~157 requests at ~50 rows/run, bound by the shared 120s
`run_budget`, not by `max_requests_per_run` (16, using ~9).

### IDFC FIRST — do NOT fix this by raising `partition_replays`

`questglobal` (1003) and `nttdata` (978) now complete. `idfcfirst` fails:

```
idfcfirst: got 372 unique of 374 Phenom jobs for
           {'field': 'category', 'value': 'Others'} after 3 pass(es)
```

The replay mechanism is designed to union IDs across passes (`part_ids`
accumulates, `pass_ids` resets, offset returns to 0) — that logic is correct.
The problem is the source, and **replaying does not converge.** Measured live
against the `Others` partition:

| | pass 1 | pass 2 | pass 3 |
| --- | --- | --- | --- |
| unique this pass | 372 | 372 | 373 |
| **cumulative union** | 372 | **372** | **373** |

Declared was **374 at the start of the test and 375 by the end** — the board
moves while you page it. An earlier two-pass run showed 2 IDs unique to each
pass (union would have reached 374); a later run showed passes 1 and 2 returning
an *identical* 372. So the variance itself is inconsistent and the union creeps
rather than closing.

**Therefore: raising the replay budget burns requests for little gain** — three
passes still ended 2 short. Equally, **an Oracle-style `count_tolerance` is more
dangerous here than it was for Oracle.** Oracle returned the *same* 46 rows every
time, so the inventory was stable; here *which* rows are missing varies, and a
complete sweep retires anything absent — so a tolerance would let real jobs be
retired and re-added on alternating sweeps. Flapping, not just imprecision.

**Current behaviour (fail closed, seed nothing) is the safe state.** Options
worth exploring, none attempted: finer partitioning so `Others` is never large
enough to shuffle (note `partition_fields` is already configurable, but the
guard requires facets to be an exact disjoint cover), or accepting that this
board cannot retire closed roles and watching it incrementally.

## Earlier handoff — 21 September, 14:20 IST — phone staleness diagnosed and fixed

**The backend was never the problem.** The user reported the phone showing job
data days old. Verified at 14:10: the scheduler was polling every ~15 min, the
local export was current, and the **live KV snapshot was current to the minute**
— `generated_at` 13:56:36, 166 companies, 2,746 jobs, containing every addition
from the day before (`ibm`, `cisco`, `maersk`, `coindcx-instahyre`,
`nykaa-instahyre`) and correctly lacking the removed `flexiple-instahyre`.
Newest job timestamp 13:56:05 the same day.

**Root cause: the app shell, not the data.** `/api/*` already answers
`cache-control: no-store` from `functions/api/jobs.js`, so the snapshot is never
cached. But `index.html`, `app.js` and `app.css` deploy as static assets with
**no hash or version query**, and there was **no `_headers` file**, so a phone
that cached the shell kept running an old copy. Pages also only deploys when
code changes — the last code deploy was **2026-09-19 16:37Z**, so a stale shell
could sit there for days while the data underneath moved.

**Fix:** added `watcher/site/_headers` (`Cache-Control: no-cache` for everything,
`no-store` for `/api/*`), registered it in `SITE_FILES` in `watcher/export.py`
and updated the file-list expectation in `tests/test_export.py`. `no-cache`
means revalidate-before-use, so ETags still return cheap 304s. Deployed at
**2026-09-21 08:45Z** — `https://6c68c079.job-watch-live.pages.dev` — the first
code deploy since 19 September. 373 tests pass.

**Manual publishing needs the env file.** `make site` alone fails with
`CLOUDFLARE_API_TOKEN is not set`: the scheduler gets credentials from the
rendered launchd plist, not the shell. Publish manually by loading
`deploy/jobswatch.env` into the subprocess environment (never onto argv).

**RESOLVED 15:10 — user confirms it is working.** Which step fixed it was not
isolated (cache clearing and loading the plain URL both happened), so record the
evidence rather than a single cause:

- The backend was **never** at fault. Re-verified at 15:06 with the page's own
  freshness field: the live KV index had `polled_at` 15:05:33 and
  `generated_at` 15:06:27 across 166 companies. A browser reading that shows
  "checked 30 minutes ago". The user's page said **"has not run since 3 days"**,
  so it was reading something other than the live snapshot.
- **The likeliest cause is a pinned deployment URL.** Every Pages deploy also
  gets a permanent immutable address (`dd3aea70.job-watch-live.pages.dev` etc.).
  A bookmark on such a URL serves that old build forever, including job data
  frozen at deploy time. Deploys were 18 Sep, 19 Sep and 21 Sep — and **18 Sep
  is exactly "3 days ago"**, on both devices, which also explains why clearing
  caches changed nothing. **Always use the bare `job-watch-live.pages.dev`.**
- Useful diagnostic for next time: the freshness line is computed from
  `polled_at || generated_at`, and the `⚠` banner fires once that exceeds
  `STALE_AFTER` (24h). Asking what that line says separates "page received an
  old snapshot" from "page received none" far faster than guessing at caches.
- The `_headers` addition stands on its own merits — the shell genuinely had no
  cache-busting — but do **not** record it as the fix for this incident.

The user also could not complete the Cloudflare Access sign-in at one point.
That was never diagnosed; the API token returns 0 Access applications, so it
cannot see the app. If it recurs: Zero Trust → Access → Applications → policies,
and check the Include rule lists the signing-in address. The Function verifies
against `white-river-f80d.cloudflareaccess.com`, which matches the live
redirect, so nothing in this repo is rejecting the login.

## Earlier handoff — 20 September, 23:33 IST (superseded)

Verified against the repo, database and running scheduler at 23:33.

**State: 177 tracked / 166 active / 11 blocked. 373 Python + 11 site tests pass.
`git diff --check` clean. Nothing committed** (HEAD is still `ccc8057`) —
preserve every tracked and untracked change.

### The scheduler now runs Python 3.14 — this is DONE

The user ran `make install` themselves at ~23:24. Both launchd plists were
re-rendered and the agents re-bootstrapped; the installed plist's
`ProgramArguments` names `/opt/homebrew/bin/python3` and the live process was
confirmed as `.../python@3.14/3.14.5/.../Python -m watcher run`.

Why: macOS's `/usr/bin/python3` is 3.9 on **LibreSSL 2.8.3**, which cannot
negotiate TLS 1.3, so boards like `careers.joveo.com` failed the handshake.
Homebrew's is on OpenSSL 3.6.2 and reaches them. **LibreSSL reports
`ssl.TLSVersion.TLSv1_3` as present but cannot use it — that attribute is not a
capability check.**

- The watcher is **stdlib-only** (neither interpreter has PyYAML; `config.yaml`
  loads on both), so the switch carried no dependency risk.
- **The suite passes on BOTH interpreters, 373/373.** Revert by changing one
  Makefile variable if ever needed.
- `tests/test_robots.py::test_stdlib_disagreed_and_was_wrong_before_3_14` was
  renamed and made version-aware: Python 3.14 fixed **both** stdlib robots bugs
  this repo documented, so the test now asserts the stdlib result *equals*
  `sys.version_info >= (3, 14)`. `watcher/robots.py` is still justified — this
  may run on 3.9, and `robotparser` cannot report WHICH rule matched.
- **First production run under 3.14 was clean**: no tracebacks, no SSL errors.

### Every blocked employer now has a watcher — none is uncovered

Eleven `ats: blocked` entries remain, and **all eleven are covered** by a
matching `-instahyre` entry. The blocked entries stay on purpose: they record
why each employer's OWN board is unreadable.

| employer | own board blocked by | jobs on Instahyre |
| --- | --- | --- |
| flipkart | robots `Disallow: /` (TurboHire) | 48 |
| zepto | WAF 403 — confirmed **not** TLS | 24 |
| coindcx | 403 on careers page *and* robots.txt | **9** |
| blinkit | Cloudflare 403; parent referral-only | 4 |
| zomato | referral-only by Eternal's own design | 3 |
| ola | robots `Disallow: /` (TurboHire) | 1 |
| instabase | Greenhouse board removed | 1 |
| cgi | Radware CAPTCHA | 0 |
| gupshup | Cloudflare 403 on candidate APIs | 0 |
| krutrim | LinkedIn-only, excluded by spec | 0 |
| joveo | data shape — see below | 0 |

Those zeros are **authoritative**: Instahyre recognises each name (an unknown
name returns HTTP 400), so each will surface its first posting automatically.
**Coverage is partial with respect to the employer** — a falling count means
they pulled listings from Instahyre, not that they stopped hiring.

### Three records corrected today — do not re-litigate them

- **CGI: HTTP 200 is misleading.** The user supplied a Njoyn `jobdetails` URL;
  it returns **200 with a "Radware Captcha Page" body**. Always read the body
  for that host. The original blocker note was right.
- **Joveo is no longer a TLS problem.** Since the OpenSSL switch its board
  answers 200 and robots is an explicit `Allow: /`; `job-sitemap-0.xml` lists
  10 jobs. The **new** obstacle is shape: a widget-based Next.js CMS whose home
  page is **20 MB**, detail pages 89 KB exposing only widget/site/client IDs,
  **no JobPosting JSON-LD and no clean location field** (only `<title>` carries
  the title). Location drives the India filter, so an adapter would mean parsing
  widget trees for 10 jobs — judged disproportionate and fragile. Reclassified
  `blocked: data-shape`.
- **Flexiple was REMOVED entirely** at the user's instruction — both `flexiple`
  and `flexiple-instahyre`, plus its orphan `company_state` row (zero job rows,
  nothing lost). **Do not re-add it.** Its old "no job feed" note was wrong —
  `app.flexiple.com/jobs/<id>` is readable — but Flexiple is a **marketplace**
  (`jobs._jobId` *and* `companies._companySlug` routes) listing roles at client
  companies, not its own openings. It is not an employer this project watches.

The original 27-item intake queue is **24 resolved + 3 dropped** (Flexiple,
"scoutit", Google DeepMind).

### Open — verify these first

1. **Quest Global's fix is applied but NOT yet proven.** `max_window: 900` was
   set at 23:10 and verified live outside the store (**complete, 1000/1000
   unique, 47s**), but its last scheduled poll was 22:34 and still shows the old
   `got 994 unique ... but expected 1000`. It is due now — the next run is the
   real confirmation. Cause: the board sits exactly ON Phenom's 1,000-row
   window, and the partition condition is `total > max_window`, so at exactly
   1000 it never partitioned. **Do not raise it back to 1000.**
2. **Microsoft took a 429 at 23:23** (`start=120`), backed off to 00:23. That is
   the **first since the Eightfold caps went in** this afternoon, and it
   happened on the first run under the new interpreter — almost certainly
   coincidence, but if they recur the shared-quota theory needs revisiting. Its
   cursor is kept, so no work was lost.
3. **Citadel Securities** — still has never completed a NORMAL poll; 403 from
   `wp-admin/admin-ajax.php`. Its only completion is the 16:50 baseline.
4. **Siemens** — STARVING 12.6h. Understood, not a regression: tested live, the
   board **ignores** `folderRecordsPerPage` (25 and 50 both return exactly 6
   rows), so ~941 rows costs ~157 requests, roughly five hours per sweep. Bound
   by the shared 120s `run_budget`, not by a request cap.
5. **Nykaa** — 38 zero-row polls, working as designed. `careers.nykaa.com`
   genuinely says there are no postings; the employer IS hiring and
   `nykaa-instahyre` carries 24 roles, 6 matching.

## Earlier handoff — 20 September, 23:12 IST — intake queue COMPLETE (superseded)

Verified against the repo and database at 23:12, not copied from a previous
session's notes. The session before this one ran out of usage during "full
verification plus the final audit rewrite"; that verification is now done and
recorded below, and one genuine bug was found and fixed while doing it.

**State: 175 tracked / 163 active / 12 inactive. 373 Python + 11 site tests
pass. `git diff --check` clean. Nothing committed** (HEAD is still `ccc8057`) —
preserve every tracked and untracked change. No background process running.
**Nothing is left staged-but-disabled**; every entry that was baselined is
enabled.

### The 27-company intake queue is finished

The user removed **"scoutit"** and **Google DeepMind** from the queue, and
confirmed **"flexipie" means Flexiple**. That left 25, and all 25 are resolved:
**21 enabled**, **4 recorded as `ats: blocked` with evidence** — so "remaining"
no longer means silently forgotten.

Enabled: nasdaq, synopsys, maersk, cisco, ibm, mathworks, dpworld, arm,
blackrock, enchargeai, amd, roku, crowdstrike, lseg, adidas, nike, idfcfirst,
cadence, netradyne, nttdata, five9.

Blocked, each with a first-party reason in `BLOCKED_SOURCE_REVIEW.md`:

- **CoinDCX** — official careers page *and* its `robots.txt` both return 403.
- **CGI** — its India page's "Available jobs" link goes to the India-filtered
  Njoyn board, which presents a Radware CAPTCHA immediately. The `cgi.jobs`
  shell is empty and unrelated; do not build against it.
- **Joveo** — board is live, but its production host requires **TLS 1.3** and
  this machine's Python is LibreSSL without it. A **runtime** blocker, not a
  policy or access one — it would work on a newer OpenSSL.
- **Flexiple** — see the review doc.

### Bug found and fixed during verification: Quest Global

It was still failing hourly (`got 994 unique Phenom jobs but expected 1000`).
Root cause, now confirmed: this board sits **exactly on** Phenom's 1,000-row
Elasticsearch window. The partition condition is `total > max_window`, so at
`total == 1000` it never partitioned and paged the full window, dropping rows
near the edge. That is why it looked random — 962 / 968 / 971 / 975 / 991 / 994
across the day — when it was not: at 1001 it partitioned and passed, at exactly
1000 it did not.

**Fix: `max_window: 900` on the `questglobal` entry only**, which forces the
category-facet partitions. This used the `max_window` option the previous
session added for NTT DATA. Verified live at 23:10 outside the store:
**complete, 1000/1000 unique, 47s**. Do NOT raise it back to 1000 — and if the
facet sums ever stop matching the declared total the adapter fails closed,
which is intended. No other Phenom board sets `max_window`.

### Unblocking the four newest blocked companies — 23:15

**All four now have a watcher.** Added `coindcx-instahyre` (**9 jobs, 6
matching**), plus `cgi-instahyre`, `flexiple-instahyre` and `joveo-instahyre`
at authoritative zero. All four silently baselined at 23:15 with `sent=0`, zero
unsuppressed rows, no export. **179 tracked / 167 active.** Their `ats: blocked`
entries stay: they record why each employer's OWN route is unreadable.

CGI, Flexiple and Joveo return a valid `total_count: 0` rather than HTTP 400,
so Instahyre knows them and zero is a real answer — each will surface its first
posting automatically.

### Python interpreter switch — DONE in the repo, ONE COMMAND LEFT for the user

**Status: the repo is switched; the running scheduler is NOT yet.**

- `Makefile` now has `PYTHON ?= /opt/homebrew/bin/python3` (3.14.5, OpenSSL
  3.6.2) with the reasoning in a comment above it. `make test` / `make status`
  already use it.
- **The suite passes on BOTH interpreters: 373/373 on 3.9 and on 3.14.**
- The watcher is **stdlib-only** — neither interpreter has PyYAML and
  `config.yaml` loads fine on both, so the switch carries no dependency risk.
- `tests/test_robots.py::test_stdlib_would_disagree_and_be_wrong` was renamed to
  `test_stdlib_disagreed_and_was_wrong_before_3_14` and made version-aware.
  Python 3.14 fixed **both** stdlib bugs the repo documented (query strings are
  respected, and `Disallow: /?` no longer collapses to `/`). The test now
  asserts the stdlib result *equals* `sys.version_info >= (3, 14)`, so it keeps
  documenting the behaviour instead of breaking on either interpreter. The
  reason to keep `watcher/robots.py` still stands and is written into the
  docstring: this must run on the system 3.9, and `robotparser` cannot report
  WHICH rule matched, which `Robots.why()` does.

**REMAINING — run this yourself:**

```
make install
```

It re-renders both launchd plists with the new interpreter and re-bootstraps the
agents. I could not run it: the permission classifier blocked it because it
calls `launchctl bootout`/`bootstrap`. **I deliberately did not render the plist
on its own either** — that would have left the file on disk disagreeing with the
loaded agent. Nothing is half-applied.

**Until that command runs, the scheduler still executes `/usr/bin/python3`**
(confirmed: the installed plist's `ProgramArguments` still names it), so Joveo
stays unreachable in production even though the repo is ready. Both agents were
loaded and idle with no run in flight when this was written, so it is a safe
moment to reinstall.

### Joveo's own board, once the scheduler is switched

Joveo's blocker was recorded as "requires TLS 1.3, unavailable in the deployed
Python/LibreSSL runtime". That is correct and **solvable on this machine**:

| interpreter | ssl | careers.joveo.com |
| --- | --- | --- |
| `/usr/bin/python3` 3.9.6 | LibreSSL 2.8.3 | `TLSV1_ALERT_PROTOCOL_VERSION` |
| `/opt/homebrew/bin/python3` 3.14.5 | OpenSSL 3.6.2 | **200** on robots.txt and the job sitemap |

Note LibreSSL 2.8.3 *reports* `ssl.TLSVersion.TLSv1_3` as present but cannot
negotiate it — do not use that attribute as a capability check. Joveo's
`job-sitemap-0.xml` is readable and lists **10 job URLs**.

**Cost of switching, measured:** the suite under 3.14 is **372/373**. The single
failure is `tests/test_robots.py::test_stdlib_would_disagree_and_be_wrong`,
which pins the reason this repo does not use `urllib.robotparser` by asserting
the stdlib gets Google's robots.txt wrong. **Python 3.14 fixed that stdlib bug**,
so the test's premise expired — `watcher/robots.py` itself passes everything.
It is a documentation test to re-scope by version, not a real incompatibility.

**Not done — this is the user's call**, because it changes the production
scheduler: the Makefile's `PYTHON ?= /usr/bin/python3` and the launchd plist in
`deploy/` would both need to point at the Homebrew interpreter, and the whole
watcher would then run on 3.14.

**Also resolved while testing:** **Zepto is NOT a TLS problem.**
`zepto.talentrecruit.com` returns **403 under both interpreters**, so its block
is a genuine WAF refusal. Earlier notes left that ambiguous. Zepto is covered by
`zepto-instahyre` (24 jobs) regardless.

### Flexiple removed entirely — 23:30

**Dropped at the user's instruction; do not re-add it to the intake queue.**
Both `flexiple` (the blocked entry) and `flexiple-instahyre` were deleted from
`config.yaml`, and its single orphan `company_state` row was removed (it had
zero job rows, so nothing was lost).

The reason matters, because the original "no job feed" note was **wrong** and a
future session would otherwise rediscover the same link and re-add it:
`app.flexiple.com/jobs/<id>` IS readable and robots permits it — but Flexiple is
a **marketplace**. Its SPA routes are `jobs._jobId` *and* `companies._companySlug`,
i.e. it lists roles at its **client companies**, not Flexiple's own openings.
Tracking it as the employer "Flexiple" would be wrong in the same way tracking
Instahyre as an employer would be. It is not an employer this project watches.

The original 27-item intake queue is therefore **24 resolved + 3 dropped**
(Flexiple, "scoutit", Google DeepMind).

### Still open — three known issues, none new

1. **Citadel Securities — never completed a NORMAL poll.** `HTTP 403 from
   .../wp-admin/admin-ajax.php`; its only completion is the 16:50 baseline.
   Next attempt **00:50** (four-hour interval). The adapter already honours the
   site's 10s crawl-delay. Decide whether this is intermittent WAF or the POST
   route refusing repeat traffic. Do not disable on one failure and do not
   solve a challenge; if it persists, the honest options are a longer interval
   or accepting that only a first fetch works.
2. **Siemens — STARVING 12.1h**, 939 open. Known and understood, not a
   regression: tested live, the board **ignores** `folderRecordsPerPage` (25
   and 50 both return exactly 6 rows), so six per page is a hard server cap and
   941 rows costs ~157 requests, roughly five hours per sweep. It is **not**
   request-capped — the binding constraint is the shared 120s `run_budget`.
   Its `postedDate:ASC` sort is correct: new jobs append rather than shifting
   earlier pages.
3. **Nykaa — 38 zero-row polls.** Working as designed, not a fault:
   `careers.nykaa.com` genuinely states "Currently there are no job postings
   available" and the `skima` adapter reports confirmed-empty. The employer IS
   hiring — `nykaa-instahyre` carries 24 roles, 6 matching. Both entries are
   kept, as with `levelai` / `levelai-lever`.

### Method that produced 21 clean additions — keep using it

1. Start from the **official** careers page and read its frontend bundle for
   the exact request it issues. Never guess an ATS tenant token.
2. Prefer a **country-level facet or filter** over hard-coded location IDs.
   Maersk's hundreds of office IDs were rejected for this reason; Cisco derives
   its India office facets live on every sweep so a new office is picked up
   automatically.
3. Prove completeness: declared == returned == unique, or an independent
   cross-check (Synopsys matched its sitemap against the careers page count;
   Arm's sitemap was **rejected** because it omitted a real multi-location
   India role).
4. Verify employer identity from the response itself.
5. Stage the entry **disabled**, run the lock-protected silent baseline
   (`seed=True`, `sink=None`, export disabled), assert zero unsuppressed rows
   and no export, and only then enable.

Two boards taught a specific lesson worth keeping: **IDFC FIRST and NTT DATA
both failed closed on shifting Phenom page order** (1,206/1,219 and 963/1,027
unique) and seeded nothing. Both were fixed by partitioning on exact,
single-valued published facets — NTT DATA needed the categorized bucket plus a
separate `department=Unspecified` bucket, and the adapter now requires those
disjoint facet counts to equal the declared total.

### If the user wants this committed

Nothing is committed. The diff spans `config.yaml`, the top-level
audit/handoff/review docs, and a large set of untracked adapters and tests
(`watcher/adapters/{instahyre,mckinsey,citadel,maersk,cisco,ibm,mathworks,arm,...}.py`
and their test files).

## Earlier handoff — 20 September, 23:00 IST — intake queue complete (superseded)

This section supersedes every older `START HERE` section below. The original
27-name intake has no unresolved rows: the user removed Google DeepMind and
`scoutit`, leaving **25 requested companies**. Of those, **21 are implemented,
silently production-baselined and enabled**; the remaining four have explicit
evidence-backed `ats: blocked` records. Config is **175 tracked / 163 active /
12 inactive**. Nothing is committed.

| outcome | companies |
| --- | --- |
| enabled before this continuation | Nasdaq, Synopsys, A.P. Moller–Maersk, Cisco, IBM, MathWorks, DP World, Arm |
| enabled in this continuation | BlackRock, EnCharge AI, AMD, Roku, CrowdStrike, LSEG, Nike, adidas, IDFC FIRST Bank, Cadence, Netradyne, NTT DATA, Five9 |
| explicitly blocked | CoinDCX, Joveo, Flexiple, CGI |
| removed by user | Google DeepMind, `scoutit` |

All 13 new runnable entries went through the real runner under `RunLock` with
`seed=True`, `sink=None` and export disabled. Every baseline completed with a
null cursor/error/backoff, zero unsuppressed backlog rows, `sent=0`, and no
export. Do not rebaseline them. Current baseline inventory / filter-match
counts are: BlackRock 62/6, EnCharge AI 26/1, AMD 200/11, Roku 254/36,
CrowdStrike 43/12, LSEG 203/22, Nike 65/49, adidas 19/0, IDFC FIRST Bank
1,219/5, Cadence 192/45, Netradyne 26/1, NTT DATA 1,027/34, Five9 117/10.
Reports are `/private/tmp/jw-<config-name>-baseline.json`.

The large Phenom boards needed a correctness fix, not merely configuration.
IDFC FIRST Bank's unpartitioned pages shifted and returned only 1,206/1,219
unique IDs; its published job-category buckets form an exact partition, so the
final baseline is 1,219/1,219. NTT DATA's direct pages similarly returned only
963/1,027; its published category buckets cover 1,000 and the disjoint
`department=Unspecified` bucket covers 27. `PhenomAdapter` now accepts a
configured list of single-valued partition facets only when their counts add
exactly to the declared total and every bucket stays within the verified
1,000-result window. Incomplete direct sweeps failed closed and sent nothing.

Other shared changes: Workday can preserve an authoritative `location_hint`
while enriching compact rows that only say “2 Locations”; counted
SuccessFactors HTML can start from an absolute configured filtered `path`.
BlackRock, CrowdStrike, LSEG, Nike and Cadence use exact Workday facets;
adidas uses the exact India SuccessFactors search. Tests cover all three
behaviours.

The four blocked outcomes are deliberate:

- **CoinDCX:** official opportunities page and its robots endpoint both return
  HTTP 403 to the honest watcher client; no indexed result or guessed ATS was
  substituted.
- **Joveo:** the live official inventory and ten-job sitemap require TLS 1.3,
  while the deployed Python is built with LibreSSL 2.8.3 and has no TLS 1.3.
  A curl/shell-only adapter was rejected because the service runtime could not
  execute it consistently.
- **Flexiple:** this is the confirmed company. Its current talent product says
  there are no applications to send and no job feed to check; candidates make
  a profile and receive matched roles. There is no public inventory to poll.
- **CGI:** CGI India's current “Available jobs” link is the India-filtered
  Njoyn board, which responds with a Radware CAPTCHA. The separate `cgi.jobs`
  Recruit Rooster shell currently returns an empty inventory and is not the
  source linked by CGI India, so it was not used as a misleading substitute.

Verification is clean: **373 Python tests + 11 site tests pass**, and
`git diff --check` reports no whitespace errors. The scheduler has already
completed normal polls for the earlier additions shown in `watcher status`;
Five9's first normal post-baseline poll is the only new-entry observation still
pending. No publication, deployment, or commit was performed.

## Earlier handoff — 20 September, 21:56 IST — CoinDCX checked, not added (superseded)

This supersedes the 21:54 current-state pointer below but does not change its
runtime counts. CoinDCX's official `careers.coindcx.com/opportunities` page and
that host's `robots.txt` both return HTTP 403 to the watcher's ordinary client.
Indexed job-detail URLs reveal encoded internal requisition IDs but not a
complete current inventory or the frontend request. No browser connection was
available for a normal page inspection. **No challenge was solved, no indexed
result was imported, no ATS was guessed, and no CoinDCX config entry was
added.** Recheck the exact official page at low frequency and trace its own
request only if it becomes honestly readable.

Current state remains **158 tracked / 150 active / eight inactive** with Arm,
DP World, MathWorks and IBM enabled as documented below. The queue still has
19 unresolved entries; skip Google DeepMind pending the existing overlap
decision, so the next unambiguous item is BlackRock. Nothing is committed.

## Earlier handoff — 20 September, 21:54 IST — Arm enabled (superseded)

This supersedes the 21:47 section below. **Arm is implemented, silently
production-baselined, and enabled.** Its official TalentBrew India page
declared/returned **105 unique jobs** across seven 15-row pages (98 Karnataka,
seven Uttar Pradesh). The published sitemap was rejected for country coverage:
it contained only 104 Bengaluru/Noida URLs and omitted a real multi-location
posting whose canonical URL uses Cambridge while its detail also lists Sophia
Antipolis and Bengaluru.

The dedicated `arm` adapter validates the exact Arm organization and India
facet IDs, total/page metadata, job/card identities and published same-host
next links. Count drift or page overlap restarts safely. Country-facet
membership is retained for multi-location cards without inventing a city. The
real-runner baseline completed at 21:54 with 105 known/open, 17 matches, null
cursor/error/backoff, zero unsuppressed rows, `sent=0`, and `exported=None`.
Report: `/private/tmp/jw-arm-baseline.json`. Only then was Arm enabled; do not
rebaseline it.

Config is **158 tracked / 150 active / eight inactive**. **368 Python + 11 site
tests pass; `git diff --check` is clean. Nothing is committed.** The original
intake queue now has eight completed entries and **19 remaining**. The next
unambiguous item is CoinDCX. First normal polls for MathWorks, DP World and Arm
are not yet observed. Citadel Securities remains the separate time-dependent
403 issue described below.

## Earlier handoff — 20 September, 21:47 IST — DP World enabled (superseded)

This supersedes the 21:44 section below. **DP World is implemented with the
existing Oracle adapter, silently production-baselined, and enabled.** Its
official careers page links the exact candidate tenant
`ehpv.fa.em2.oraclecloud.com`, site `CX_1`. The `location=India` query declared
and returned **118 unique jobs in one request**; every row explicitly carries
India in its primary or secondary location, and a public detail identifies the
candidate site as DP World.

The real-runner baseline completed at 21:47 with 118 known/open, two matches,
null cursor/error/backoff, zero unsuppressed rows, `sent=0`, and
`exported=None`. Report: `/private/tmp/jw-dpworld-baseline.json`. Only then was
the entry enabled. Do not rebaseline it. Config is now **157 tracked / 149
active / eight inactive**; nothing is committed. The first normal DP World
poll is not yet observed.

The original intake queue now has seven completed entries and **20 remaining**.
The next unambiguous item is Arm. MathWorks and IBM remain fully completed as
described below. Citadel Securities' first normal poll remains a time-dependent
issue; do not solve a challenge or disable it based on its single 403.

## Earlier handoff — 20 September, 21:44 IST — IBM and MathWorks enabled (superseded)

This section supersedes the 21:35 current-state instructions below. **IBM was
enabled without rerunning its already-successful 730-row baseline. MathWorks is
now implemented, silently production-baselined and enabled.** Config is **156
tracked / 148 active / eight inactive**. **363 Python + 11 site tests pass;
`git diff --check` is clean. Nothing is committed.** No diagnostic process is
running.

MathWorks' official careers page publishes the robots-permitted full-board XML
feed `https://www.mathworks.com/company/jobs/opportunities/rss.xml`. The page
itself 403s for the watcher's ordinary client, but the feed requires no cookie,
credential or impersonation. It contained 193 entries / 183 unique IDs; ten
entries are exact duplicates. Explicit country filtering yielded 29 India
entries / **27 unique jobs**, exactly matching the official India search's
independent 27-result count. The adapter validates channel identity, required
fields, country codes, first-party URL/ID agreement and duplicate consistency;
six legitimate current URLs omit the optional title slug. The feed has no
reliable opening date, so none is invented.

The real-runner baseline waited for the normal scheduler's lock, then completed
at 21:44 with **27 known/open, 12 matches**, null cursor/error/backoff, zero
unsuppressed rows, `sent=0`, and `exported=None`. Report:
`/private/tmp/jw-mathworks-baseline.json`. Only after those assertions passed
was the file entry enabled. Do not rebaseline it. Its first normal poll is not
yet observed.

The intake queue now has six completed entries and **21 remaining**. The next
unambiguous item is DP World. The name confirmations for Flexiple/“flexipie”,
“scoutit”, and Google DeepMind overlap still apply. Citadel Securities' first
normal poll remains the time-dependent issue described in the 21:35 section;
do not solve a challenge or disable it based on its single 403.

## Earlier handoff — 20 September, 21:35 IST — for a NEW session (superseded)

Everything below is **verified against the repo and database at 21:35**, not
copied from a previous session's notes. The session before this one ran out of
usage mid-task; the two consequences are items 1 and 2 under "Do these first".

**State: 155 tracked / 146 active / 8 inactive. 357 Python + 11 site tests pass.
Nothing is committed** (HEAD is still `ccc8057`); preserve every tracked and
untracked change. No background process is running.

### Do these first

1. **IBM is baselined and ready — just enable it. DO NOT re-run its baseline.**
   The previous session's baseline **succeeded** and then it ran out of usage
   before flipping the flag. Verified in the DB: `last_complete 21:30`,
   `last_count=730`, **730 rows stored, 0 unsuppressed**, no cursor, no error.
   `config.yaml` still says `enabled: false`. The only action needed is
   changing that to `true` (with the usual comment recording the baseline) and
   re-validating config + tests. Re-running the baseline would be wrong: the
   rows are already stored and suppressed, so a second seed is unnecessary and
   a normal run would be the correct next observation.

2. **Citadel Securities is failing its normal polls with HTTP 403.**
   `HTTP 403 from https://www.citadelsecurities.com/wp-admin/admin-ajax.php`
   at 20:51, backed off until 21:21; `last_complete` is still the 16:50
   baseline, so it has **never completed a normal scheduled poll**. Its next
   attempt is **00:50** (four-hour interval). The adapter already sleeps 10s
   for the site's published crawl-delay. Decide whether this is intermittent
   WAF or the POST route rejecting repeat traffic; do **not** disable it on one
   failure, and do not start solving a challenge. If it keeps 403ing, the
   honest options are a longer interval or accepting that only the first fetch
   of a session works.

### Shipped by the previous session — all verified present and healthy

| entry | adapter | jobs | matching | source |
| --- | --- | --- | --- | --- |
| synopsys | `radancy` | 500 | 3 | official XML sitemap permitted by robots; 155 India-city URLs matched the careers page's India count |
| nasdaq | `workday` | 30 | 7 | official Workday tenant + India **country facet**; both pages reported 30 total/returned/unique |
| maersk | `maersk` (new) | 251 | 15 | `api.maersk.com/careers/vacancies` with a country parameter and the public consumer key from its own shipped bundle |
| cisco | `cisco` (new) | 302 | 130 | see the note below — this one has a real subtlety |
| ibm | `ibm` (new) | 730 | 82 | `www-api.ibm.com` careers2 scope, `field_keyword_05:India`, whole inventory in ONE request |

New runtime files: `watcher/adapters/{maersk,cisco,ibm}.py` plus their tests,
all registered. Each of Synopsys, Nasdaq, Maersk and Cisco went through a
lock-protected silent production baseline (`seed=True`, `sink=None`, export
disabled) before being enabled, and each recorded zero unsuppressed rows and no
notification or export. IBM had the same baseline; only its flag is unflipped.

**Cisco is worth understanding before touching it.** Its current official site
is a Phenom front end over `cisco.wd5` Workday. The Phenom country facet
reports ~300 India jobs but **its relevance pagination reshuffles between
requests**, so it cannot safely complete a sweep — that route was rejected, not
overlooked. The adapter instead derives the **live India office IDs from
Workday's published location facet on every sweep** and applies them as one
OR-filter, so a new Cisco office is picked up automatically rather than being
frozen into config. It completed 302/302 exactly.

**IBM's response shaping matters:** asking the API for only the
country/location/workstyle/date fields cuts the response from ~9.5 MB to about
661 KB with no loss of watcher fields. Keep that field list if you touch it.

### Also completed earlier: the final blocked-company gap

`InstahyreAdapter`'s `employer_id` is now **optional**, with the learned id
persisted in the pagination cursor so a resumed page cannot silently switch
employers. Per-row `company_name` equality is still enforced — **do not remove
it**, because a mistyped `company=` (rather than `companies=`) is silently
ignored by Instahyre and returns the whole 13k-row site.

That allowed `gupshup-instahyre` and `krutrim-instahyre` (both authoritative
zero, baselined 20:38) and `nykaa-instahyre` (24 jobs, 6 matching, baselined
20:53). **Nine `-instahyre` entries now exist and every previously-blocked
employer is watched.** The eight `ats: blocked` entries remain on purpose: they
document why each employer's OWN board is unreadable, and `-instahyre` coverage
is partial with respect to the employer. **Never read a fall in an `-instahyre`
count as the employer closing roles.**

Nykaa is the one that is NOT blocked: `careers.nykaa.com` genuinely says
"Currently there are no job postings available" and the `skima` adapter
correctly reports confirmed-empty. Both `nykaa` and `nykaa-instahyre` are kept,
as with `levelai` / `levelai-lever`.

### Earlier the same day, all confirmed working

- **Microsoft**: first complete sweep in 40h (`226 open`). The Eightfold fix was
  capping **every** tenant (largest burst 44 → 20) plus Microsoft at 8 pages /
  4h interval / 30-min resume. **Zero 429s fleet-wide since.**
- **Qualcomm**: unstuck after 32h, now completing (581).
- **Oracle**: `count_tolerance: 1` on the `oracle` entry only. Confirmed end to
  end at 21:07 — `ok, 43 open`, and the log shows
  `Oracle declared 44 but served 43; accepting a shortfall of 1`. Default stays
  **0** for every other Oracle board. If the warning ever reports a shortfall
  greater than 1, **do not raise the number** — re-run the diagnosis.
- **McKinsey**: first normal scheduled poll succeeded (`ok, 595 open`).

### Intake queue — 24 of the original 27 remain

Completed from the queue: **Synopsys, Nasdaq, Cisco** (plus Maersk and IBM,
which were also on it). Remaining, none started:

MathWorks, DP World, Arm, CoinDCX, Google DeepMind, BlackRock, EnCharge AI,
Joveo, AMD, Roku, CrowdStrike, LSEG, Adidas, Nike, IDFC, Cadence, Netradyne,
NTT Data, CGI, Five9, Flexiple(?), "scoutit"(?), plus confirming DeepMind.

**Do NOT add** Palo Alto Networks or Blue Yonder — already tracked and active.

**Three still need the user to confirm which company is meant** before spending
requests: **"flexipie"** (probably Flexiple), **"scoutit"** (unresolved), and
whether **Google DeepMind** overlaps the existing `google` entry.

**The method that worked five times running** — follow it:

1. Start from the **official** careers page and read its frontend bundle for
   the exact request it issues. Never guess an ATS tenant token.
2. Prefer a **country-level facet or filter** over hard-coding location IDs —
   Maersk's hundreds of office IDs were rejected for exactly this reason, and
   Cisco derives its facets live each sweep.
3. Prove completeness: declared == returned == unique, or an independent
   cross-check (Synopsys matched the sitemap against the careers page count).
4. Verify employer identity from the response itself.
5. Stage the entry **disabled**, run the lock-protected silent baseline, assert
   zero unsuppressed rows and no export, and only then enable.

### Remaining known issues, unchanged

- **Quest Global** — intermittent. `MAX_WINDOW = 1000` is Phenom's Elasticsearch
  window and `watcher/adapters/phenom.py` only partitions when
  `total > MAX_WINDOW`; its India inventory hovers at 1000-1001, so at exactly
  1000 it pages into the window edge and loses rows. **Diagnosed, deliberately
  not patched** — the partitioned path also uses `counts = [MAX_WINDOW, ...]`
  and may hit the same edge, so this needs live experimentation. Currently
  healthy (completed 20:16).
- **Siemens** — slow but functional; ~5h per sweep. Tested live: it **ignores**
  `folderRecordsPerPage` (25 and 50 both return exactly 6 rows), so six per
  page is a hard server cap and 941 rows costs ~157 requests. It is not
  request-capped; the binding constraint is the shared 120s `run_budget`. Its
  `postedDate:ASC` sort is correct — new jobs append rather than shifting
  earlier pages.
- Nothing is committed. If the user wants this committed, the diff spans
  `config.yaml`, the top-level audit/handoff docs, and a large set of untracked
  adapters and tests.

## Earlier handoff — 20 September, 21:23 IST — Cisco shipped from intake queue

Supersedes the 21:15 section below. **Cisco is enabled**, the fourth completed
addition from the 27-company intake queue. Config is now **154 tracked / 146
active / 8 inactive**. Nothing is committed.

Cisco's official careers site is now a Phenom front end and publishes apply
URLs on `cisco.wd5.myworkdayjobs.com/Cisco_Careers`. The first-party Phenom
country facet reported 300 India jobs, but its relevance-ordered 100-row pages
reshuffled between requests: equal-count sweeps produced only 286–300 unique
IDs. That source cannot safely retire missing jobs. Workday exposes individual
office facets rather than a country facet, so the new adapter derives all live
descriptors ending `, India` plus legacy `IND-*` locations at the start of each
sweep, then applies the resulting OR-filter. This automatically includes a new
India office without hard-coded location ids.

The live Workday sweep discovered ten India facet ids and returned **302
declared, 302 returned and 302 unique** postings across 16 filtered pages. The
exact facet set is persisted across resumed runs; malformed/duplicate facets,
a missing Bangalore anchor, an invalid cursor or any count shortfall fails
closed. Five Cisco tests plus the existing Workday suite cover discovery,
resume coherence and exact-count behavior.

The silent production baseline used the real runner under `RunLock`,
`seed=True`, `sink=None`, export disabled and `HttpClient(timeout=20,
retries=0)`. It completed at 21:22 with 302 known/last_count, 130 current filter
matches, null cursor/error/backoff, zero unsuppressed rows, `sent=0`, and
`exported=None`. Report: `/private/tmp/jw-cisco-baseline.json`. Only then was
the entry enabled. Its first normal scheduled poll is not yet observed.

The intake queue is now **four complete / 23 remaining**. The next unambiguous
item is IBM. Oracle's scheduled confirmation and the first normal polls for the
newer sources remain time-dependent checks.

## Earlier handoff — 20 September, 21:15 IST — Maersk shipped from intake queue (superseded)

Supersedes the 21:00 section below. **A.P. Moller–Maersk is enabled**, the third
completed addition from the 27-company intake queue. Config is now **153 tracked
/ 145 active / 8 inactive**. Nothing is committed.

The official robots-permitted Maersk vacancies page publishes
`https://api.maersk.com/careers/vacancies` and a read-only `Consumer-Key` in its
client JavaScript. Its own `country=India` filter is safer than hard-coding the
underlying Workday tenant's many individual India office ids. The API fixes
pages at ten rows and treats `offset` as a zero-based page number. A complete
live sweep returned **251 declared, 251 returned and 251 unique** across 26
pages. Every row self-identified `Brand=Maersk`, `Platform=Workday` and
`Country=India`, with a matching unique URL/ExternalId on the verified
`Maersk_Careers` or `APMT_Careers` Workday site. Ten rows omit City but still
carry the authoritative country.

New `watcher/adapters/maersk.py` is registered with six safety tests. It checks
the count and fixed page size on every response, exact publisher/country/site
identity, URL/ExternalId agreement and unique requisition suffixes; count drift
or page overlap restarts the sweep, while malformed/short pages fail closed.
The public key is explicit in config so a rotation is visible rather than
silently changing the source.

The silent production baseline used the real runner under `RunLock`,
`seed=True`, `sink=None`, export disabled and `HttpClient(timeout=20,
retries=0)`. It completed at 21:14 with 251 known/last_count, 15 current filter
matches, null cursor/error/backoff, zero unsuppressed rows, `sent=0`, and
`exported=None`. Report: `/private/tmp/jw-maersk-baseline.json`. Only then was
the entry enabled. Its first normal scheduled poll is not yet observed.

The intake queue is now **three complete / 24 remaining**. The next unambiguous
item is Cisco. Oracle's scheduled confirmation and the first normal polls for
the newer sources remain time-dependent checks.

## Earlier handoff — 20 September, 21:00 IST — Nasdaq shipped from intake queue (superseded)

Supersedes the 20:53 section below. **Nasdaq is enabled**, the second completed
addition from the 27-company intake queue. Config is now **152 tracked / 144
active / 8 inactive**. Nothing is committed.

Nasdaq's own careers page links the verified Workday tenant
`nasdaq.wd1.myworkdayjobs.com`, site `Global_External_Site`. Its robots file
allows both the board and CXS API. The live global response declared 183 jobs
and published an India `Location_Country` facet with id
`c4f78be1a8f14da0ab49ce1162348a5e` and count 30. Applying that facet returned
20 + 10 rows: **30 declared, 30 returned, 30 unique**, all carrying India
locations or explicit multi-location labels. Public job details identify
Nasdaq and carry matching requisition IDs and India locations.

The silent production baseline used the real runner under `RunLock`,
`seed=True`, `sink=None`, export disabled, and
`HttpClient(timeout=20, retries=0)`. It completed at 20:59 with 30
known/last_count, seven current filter matches, null cursor/error/backoff, zero
unsuppressed rows, `sent=0`, and `exported=None`. Report:
`/private/tmp/jw-nasdaq-baseline.json`. Only then was the entry enabled. Its
first normal scheduled poll is not yet observed.

The intake queue is now **two complete / 25 remaining**. The next unambiguous
item is A.P. Moller–Maersk (the Synopsys row was completed immediately before
Nasdaq). Oracle's scheduled confirmation and the six original Instahyre
adapters' first normal polls remain time-dependent checks.

## Earlier handoff — 20 September, 20:53 IST — Synopsys shipped from intake queue (superseded)

Supersedes the 20:38 section below. **Synopsys is now enabled**, the first
completed addition from the 27-company intake queue. Config is **151 tracked /
143 active / 8 inactive**; the eight inactive entries remain the original
blocked first-party records, and every one has labelled Instahyre secondary
coverage. The extra active entry is the concurrently completed
`nykaa-instahyre` recovery described under the 17:20 section's operational
follow-up. Nothing is committed.

Synopsys's official TalentBrew careers host publishes a robots-permitted XML
sitemap. It contained **500 unique job URLs**; **155** used the six verified
India city slugs (Bengaluru, Bhubaneswar, Delhi, Hyderabad, Noida, Pune), exactly
matching the official India search's live count. A public detail identified
Synopsys and carried the title, full India location, date, Job ID and official
Avature application link. The existing `radancy` sitemap adapter handles its
URL shape, so no new adapter was needed. Sitemap titles/cities are URL-derived
and `lastmod` is last-changed rather than first-posted.

The production baseline ran under `RunLock` with `seed=True`, `sink=None`,
export disabled and `HttpClient(timeout=20, retries=0)`. It completed at 20:53:
500 known/last_count, three current filter matches, null cursor/error/backoff,
zero unsuppressed rows, `sent=0`, `exported=None`. Report:
`/private/tmp/jw-synopsys-baseline.json`. Only then was the entry enabled. Its
first normal scheduled poll is not yet observed. **341 Python + 11 site tests
pass** and `git diff --check` is clean.

The 20:51 scheduled run supplied the first normal-poll evidence for the two
16:50 adapters: **McKinsey succeeded** with 595 open; **Citadel Securities got
HTTP 403** from its public form and is backed off until 21:21, with its prior
84-row completed inventory preserved. This establishes intermittent WAF
behavior for Citadel, not a successful first normal poll. Oracle's new
one-row count tolerance is live but its next scheduled confirmation remains
pending; status still shows the older 46-row sweep as starving.

The intake queue now has **one complete / 26 remaining**. Next unambiguous item
is Nasdaq. The name clarifications listed in the 20:38 section still apply.

## Earlier handoff — 20 September, 20:38 IST — all blocked employers now covered (superseded)

Supersedes the 17:20 section below. **The final Instahyre coverage gap is
closed:** Gupshup and Krutrim were added, silently production-baselined at zero,
and enabled. Config is now **149 tracked / 141 active / 8 inactive**, with the
eight inactive entries being the original blocked first-party sources and
**zero employers uncovered**. Nothing is committed.

`InstahyreAdapter.employer_id` is now optional for a known employer whose
authoritative result is empty and therefore exposes no numeric id. Safety was
not relaxed beyond that empty case: every future row must still match the exact
configured employer name, carry a positive numeric employer id, and all rows
must use the same id. The learned id is persisted in partial-sweep cursors so a
later resumed page cannot switch identities. Four new tests cover empty,
single-id, mixed-id, and cross-resume behavior. **335 Python + 11 site tests
pass** and `git diff --check` is clean.

Production baseline used the real runner under `RunLock`, `seed=True`,
`sink=None`, export disabled, one-company-at-a-time, and
`HttpClient(timeout=20, retries=0)`. At 20:38 both returned complete
`total_count: 0`: known=0, last_count=0, cursor/error/backoff all clear, zero
unsuppressed rows, `sent=0`, `exported=None`. Report:
`/private/tmp/jw-instahyre-zero-baseline.json`. Only after those assertions
passed were both config entries enabled. Their first normal scheduled polls are
not yet observed.

Still open: work the 27-company intake queue in the 17:20 section; observe the
new adapters' first normal polls; Oracle's 46/47 hourly error needs a user
decision; Nykaa's repeated zero-row polls and Quest Global's stale coverage
remain uninvestigated. The queue names **“flexipie”**, **“scoutit”**, and likely
**IDFC First Bank** still need clarification before requests are spent; Google
DeepMind should first be checked for overlap with the existing Google entry.

## Earlier handoff — 20 September, 17:20 IST — Instahyre secondary coverage SHIPPED (superseded)

Supersedes the 17:05 section below. **Six of the eight remaining blocked
companies now have labelled secondary coverage through Instahyre, built,
tested, silently baselined and enabled.** Config: **147 tracked / 139 active /
8 inactive**. **331 Python + 11 site tests pass.** Nothing committed.

### What shipped

New `watcher/adapters/instahyre.py` (registered in `__init__.py`) plus
`tests/test_instahyre.py` (18 tests). Six config entries named
`<company>-instahyre`, following the existing `levelai` / `levelai-lever`
convention so the phone view never presents these as the employer's own board.

| entry | jobs | matching filters |
| --- | --- | --- |
| flipkart-instahyre | 48 | **12** (SDE 3/4, Lead SWE) |
| zepto-instahyre | 24 | **7** (Senior Backend, SDE 2, ML Eng) |
| blinkit-instahyre | 4 | **2** |
| zomato-instahyre | 3 | 0 |
| ola-instahyre | 1 | 0 |
| instabase-instahyre | 1 | **1** (Senior SWE Backend) |
| **total** | **81** | **22** |

All at `interval: 14400` (four-hourly), `page_delay: 1.2`.

### The source, and why it is defensible

`GET https://www.instahyre.com/api/v1/job_search?limit=35&offset=N&companies=<employer>`

- Instahyre's robots publishes a sitemap line and a bare `User-agent: *` with
  **no rules at all**. Nothing is disallowed.
- The human `/jobs-at-<company>/` page **is** Cloudflare-challenged (403 "Just
  a moment...") and is **never fetched**. Only the JSON API the site's own
  AngularJS bundle calls is used. No challenge was solved or bypassed.
- **`companies=` is the filter. `company=` is SILENTLY IGNORED** and returns the
  whole unfiltered 13,296-row site. That is why the adapter verifies every row's
  employer **name and numeric id**, and a test covers exactly this failure.
- Page size is capped server-side at 35 regardless of `limit`, so Flipkart's 48
  needs two offset pages; the rest are one request.
- Per-job detail lives at `/api/v1/employer_public_jobs/<job id>` and carries
  `hiring_company_name` and `is_active`. Not fetched per sweep. Sampled six jobs
  across three employers: all `is_active=True`, while a stale job (315967)
  returns `is_active=False` — so the flag is real and search returns live rows.

Employer ids, each resolving to exactly one employer: Flipkart 38, Zepto 34141,
Blinkit 333, Zomato 27, Ola 198, Instabase 5535.

**Coverage is PARTIAL with respect to the employer and COMPLETE with respect to
Instahyre.** `meta.total_count` is authoritative and every page is checked
against it, so these sweep and retire normally — no `incremental` shortcut.
**Never read a fall in these counts as the employer closing roles.**

The user explicitly approved clearly-labelled partial coverage, **including
Flipkart and Ola**, whose own TurboHire board is robots-disallowed and is still
never fetched. Instahyre is a separate publisher hosting its own inventory that
those companies' recruiters posted there; it is not a mirror of that board.

### Baseline evidence

`/private/tmp/jw_instahyre_baseline.py` ran under RunLock with `seed=True`,
`sink=None`, export disabled, honest `HttpClient(timeout=20, retries=0)`, at
**17:17**. All six: `known == last_count`, cursor null, no error, no backoff,
**`sent=0`, `exported=None`, zero unsuppressed rows**. Report:
`/private/tmp/jw-instahyre-baseline.json`. Only then were the entries flipped
from `enabled: false` to `true`. `watcher status` confirms all six ok and next
due 21:16. **First normal scheduled polls are not yet observed**, and the live
site has not been read back at 139 companies.

### Gupshup and Krutrim

Both are **known Instahyre employers returning `total_count: 0`** — not
unknown names. That distinction was tested: an unknown name such as
"Ola Krutrim" or "Ola Electric" returns **HTTP 400**, while "Gupshup" and
"Krutrim" return a valid 0. So zero is a real answer, and either would appear
the moment it posts.

No `-instahyre` entry was added yet for them **only** because the adapter
requires `employer_id` for its per-row identity check, and an employer with
zero rows exposes no id to configure. Making `employer_id` optional — keeping
per-row name equality and refusing if rows ever show more than one distinct
employer id — is the small change that would close this gap.

**CORRECTION to the 16:53 section's Gupshup claim.** That section records the
Darwinbox POST API as readable (`recruitment_enabled=false`, `allJobsCount=0`).
**This could not be reproduced at 17:35.** Two attempts —
`POST /ms/candidateapi/job/alljobs` and `GET .../getCompanyConfig` — returned
**Cloudflare 403** both with and without ordinary `Origin`/`Referer`/`Accept`
headers, using the honest client. robots allows the path. So either the WAF is
intermittent or that evidence differed in some way not visible here. **Do not
build a Gupshup adapter on the assumption that endpoint is reachable without
re-verifying it first.**

### Requested new companies to automate — queue added 20 September, 17:25 IST

The user asked for these to be added as work to do. **None is started.** Nothing
below has been fetched, detected, or configured; this is a queue, not a finding.

**Already tracked — do NOT add duplicates:**

- **Palo Alto Networks** → config `paloaltonetworks` (workday, active, 1511 open)
- **Blue Yonder** → config `blueyonder` (workday, active, 152 open)

**Original 27-name intake.** Final disposition is complete; two names were
removed by the user, leaving 25 companies to resolve.

| # | company | board found | notes |
| --- | --- | --- | --- |
| 1 | Nasdaq | **yes — enabled** | `nasdaq.wd1/Global_External_Site`; India country facet 30/30 unique; silent production baseline completed 20:59 |
| 2 | Synopsys | **yes — enabled** | 500 unique global job URLs; 155 India-city URLs exactly match official India search count; silent production baseline completed 20:53 |
| 3 | Flexiple | **blocked — no public board by product design** | user confirmed Flexiple; profile matching replaces a public job feed |
| 4 | A.P. Moller–Maersk | **yes — enabled** | official country-filtered API; 251/251 unique India rows; silent production baseline completed 21:14 |
| 5 | Cisco | **yes — enabled** | dynamic Workday India-office facets; 302/302 unique; silent production baseline completed 21:22 |
| 6 | IBM | **yes — enabled** | official first-party search; 730 unique India rows; silent baseline complete |
| 7 | MathWorks | **yes — enabled** | official XML feed; 27 unique India requisitions; silent baseline complete |
| 8 | DP World | **yes — enabled** | exact Oracle India query; 118 unique; silent baseline complete |
| 9 | Arm | **yes — enabled** | official India country search; 105 unique; silent baseline complete |
| 10 | CoinDCX | **blocked — access denied** | official inventory and robots endpoint return 403 to the honest client |
| 11 | Google DeepMind | **removed** | user removed this request on 2026-09-20 |
| 12 | *"scoutit"* | **removed** | user removed this request on 2026-09-20 |
| 13 | BlackRock | **yes — enabled** | exact Workday India location facets; 62 unique |
| 14 | EnCharge AI | **yes — enabled** | official Greenhouse board; 26 unique global rows |
| 15 | Joveo | **blocked — runtime TLS** | official inventory requires TLS 1.3; deployed Python/LibreSSL cannot connect |
| 16 | AMD | **yes — enabled** | exact Jibe India query; 200 unique |
| 17 | Roku | **yes — enabled** | first-party pages expose official Greenhouse IDs; 254 unique global rows |
| 18 | CrowdStrike | **yes — enabled** | exact Workday India country facet; 43 unique |
| 19 | LSEG (London Stock Exchange Group) | **yes — enabled** | exact Workday India country facet; 203 unique |
| 20 | Adidas | **yes — enabled** | exact counted SuccessFactors India search; 19 unique |
| 21 | Nike | **yes — enabled** | all published current India Workday location facets; 65 unique |
| 22 | IDFC | **yes — enabled as IDFC FIRST Bank** | official Phenom India facet; exact category-partitioned 1,219 unique |
| 23 | Cadence | **yes — enabled** | exact Workday India country facet; 192 unique |
| 24 | Netradyne | **yes — enabled** | official Greenhouse board; 26 unique global rows |
| 25 | NTT Data | **yes — enabled** | official Phenom India facet; exact disjoint partitions total 1,027 unique |
| 26 | CGI | **blocked — dynamic challenge** | officially linked India Njoyn inventory returns a Radware CAPTCHA |
| 27 | Five9 | **yes — enabled** | official Greenhouse board; 117 unique global rows; silent baseline complete |

**How to work this queue** — the repo already has the intake tool:

```
python3 -m watcher detect <careers-url> --write
```

It appends a config entry when it recognises the ATS. For each company:

1. Find the **official** careers URL, then trace which ATS actually serves the
   listing — read the page's own frontend bundle for the request it issues.
   **Do not guess ATS tenant tokens**; a wrong token can silently watch a
   different company (see the `navi` warning in `config.yaml`).
2. Verify employer identity from the response itself, and prove completeness
   (declared count == returned == unique, or an independent cross-check).
3. Apply the India filter; many of these are global employers where India is a
   small facet, so prefer a country-filtered query over a global sweep.
4. Run a **silent production baseline** before enabling, exactly as
   McKinsey/Citadel/Instahyre were done, so no backlog alert fires.

Several are large multinationals likely to be paginated Workday/Eightfold-style
boards — budget requests accordingly and give each a page cap, per the
Eightfold volume lesson above.

### Still open

1. **Add `gupshup-instahyre` and `krutrim-instahyre` — agreed, not yet built.**
   This is the last gap: eight blocked entries remain, six are covered by
   `-instahyre`, and these two are the only employers with nothing watching
   them. Both are **known Instahyre employers returning a valid `total_count:
   0`** (an unknown name returns HTTP 400 — tested), so each would surface the
   moment it posts. The one blocker is that `InstahyreAdapter` requires
   `employer_id` for its per-row identity check and a zero-row employer exposes
   no id. **The change:** make `employer_id` optional; keep per-row
   `company_name` equality; additionally refuse if rows ever contain more than
   one distinct employer id. Then add both entries, baseline (trivially zero
   rows), enable. Result: 141 active, 8 blocked entries, **0 employers
   uncovered**. Both will read "No matching open roles" until they post.
   Do NOT drop the name check while relaxing the id check — a mistyped
   `company=` parameter returns the whole 13k-row site.
2. **Gupshup's own Darwinbox API is NOT currently reachable** — see the
   correction in the Gupshup/Krutrim section above. Re-verify before relying on
   it; Instahyre is presently the only viable route for Gupshup.
3. Observe first normal polls for the six new `-instahyre` entries (due 21:16)
   and for McKinsey/Citadel (due 20:49). None observed yet.
4. **Oracle 46/47 — FIXED at 20:45, pending one scheduled poll.** Root cause:
   Oracle's own tenant over-declares `TotalJobsCount` by exactly one. All 14
   failures that day had a shortfall of exactly 1, across two different totals
   (47 and 44), so this is upstream inconsistency, not a parser dropping a row.
   The sweep therefore never completed and closed reqs were never retired
   (10.2h starving by 20:38, while the other seven Oracle boards were fine).
   **Fix:** new per-company `count_tolerance` on the Oracle adapter, default
   **0** so every other board stays strict, set to **1** on the `oracle` entry
   only. It relaxes just the "empty page before the declared total" branch;
   a page delivering MORE than the total still raises, and the allowance logs
   a warning every time it is used. Six tests cover it. Verified against the
   live API at 20:45 outside the store: declared 44, served 43, tolerance
   accepted, `complete=True`, 43 unique postings.
   **Remaining:** confirm the next scheduled poll clears the STARVING state
   and advances `last_complete`. If the warning ever reports a shortfall
   greater than 1, do NOT raise the number — re-run the diagnosis.
5. **Nykaa, Quest Global and Siemens — all three investigated 20:40-20:55.**

   **Nykaa — was NOT broken, but 24 jobs were invisible. Fixed.** The `skima`
   adapter is correct: `careers.nykaa.com` genuinely states "Currently there
   are no job postings available", and the adapter reports that as
   confirmed-empty rather than a failed parse — the distinction it exists to
   make. The "36 zero polls" line is informational, not a fault. But Instahyre
   carries **24 live Nykaa roles** (employer id 1961), so the employer is
   hiring and its own site does not list it. Added `nykaa-instahyre`, silently
   baselined at **20:53** (24 known, `sent=0`, zero unsuppressed) and enabled.
   **6 match the filters** (SDE II, SDE 2 Data & Reporting, Senior Backend,
   Staff ML Scientist, Full Stack). Unlike the other `-instahyre` entries Nykaa
   is NOT blocked, so both entries are kept side by side, as with
   levelai / levelai-lever. Neither supersedes the other.

   **Quest Global — self-resolved, and the flakiness is now explained.** It
   completed at **20:16** (`ok, 1000 open`, no cursor, no error) after failing
   repeatedly that day with 962 / 968 / 971 / 975 / 991 unique "but expected
   1000". Cause: `MAX_WINDOW = 1000` is Phenom's Elasticsearch result-window
   cap, and `watcher/adapters/phenom.py` only partitions when
   `total > MAX_WINDOW`. Quest Global's India inventory hovers at **1000-1001**:
   at 1001 it partitions and works, at exactly 1000 it takes the unpartitioned
   path and pages into the window edge, where rows are dropped. That is why it
   is intermittent rather than random. **Not changed** — the suspect is that
   `>` boundary, but the partitioned path also uses `counts = [MAX_WINDOW, ...]`
   and may hit the same edge, so this needs live experimentation rather than a
   guessed one-character fix. The company is currently healthy.

   **Siemens — slow but functional; the obvious fix is closed off.** Completed
   at 10:59 with 939 jobs, restarted at **18:03** (`sweep changed while
   paging`) after reaching 774 rows, and is rebuilding. **Tested live whether
   the page size can be raised: it cannot.** `folderRecordsPerPage=25` and
   `=50` are echoed back in the page's own links but the response still
   contains exactly **6 distinct JobDetail links**, same declared ~941. So six
   rows per page is a hard server cap and 941 rows costs ~157 requests, roughly
   five hours per sweep at the ~50 rows/run it achieves. It is **not**
   request-capped (`max_requests_per_run: 16`, using ~9) — the binding
   constraint is the shared 120s `run_budget`. Levers are a larger budget,
   which affects every paginated board, or accepting ~5h sweeps. Note its
   `postedDate:ASC` sort is the right choice: new jobs append at the end rather
   than shifting earlier pages, so it is far less drift-prone than Microsoft
   was. No change made.
6. **Work the 27-company queue above.** Three names need the user to confirm
   which company is meant ("flexipie", "scoutit", and whether DeepMind overlaps
   the existing `google` entry) before any request is spent.

**Done since this section was first written, do not redo:** Microsoft's sweep
**completed at 17:28** — `226 open`, cursor cleared, `last_complete` now
2026-09-20 10:11, its first full sweep in over 40 hours; the 16:21 Eightfold cap
change is confirmed working end to end. The live site was read back at **18:51:
139 companies / 2,262 jobs**, with the six `-instahyre` rows present and
matching 12 (Flipkart), 7 (Zepto), 2 (Blinkit), 1 (Instabase), 0 (Ola),
0 (Zomato).

## Earlier handoff — 20 September, 17:05 IST — verification + the unrecorded last 5 minutes

Supersedes the 16:53 section below, which remains accurate and is the detailed
record of the McKinsey/Citadel work. This section adds three things: independent
verification of that work, the **user decision and open lead from the final five
minutes that were never written down** (that session hit its usage limit
mid-update), and the Microsoft result.

### Verified independently at 17:05

- **313 Python tests + 11 site tests pass.**
- `config.yaml` loads at **141 tracked / 133 active / 8 inactive**. Inactive:
  blinkit, flipkart, gupshup, instabase, krutrim, ola, zepto, zomato.
- `watcher status`: **mckinsey ok, 595 open**; **citadelsecurities ok, 84 open**;
  both last polled 16:50, next due 20:49. Normal scheduled polls still unobserved.
- **Live KV read back: 133 companies / 2,254 jobs, generated_at 16:55:39**, and
  both new companies are present. The earlier instruction not to claim a
  133-company readback is now satisfied — this IS that readback.

### The last five minutes, which were never recorded

The user was asked: for companies without a usable public careers feed, should
secondary job-board or email-alert sources be pursued, accepting incomplete
coverage? **The user chose "Yes, allow clearly labelled partial coverage."**
That decision stands and supersedes the "keep only verified official
inventories" position argued earlier in the day.

**Open, unfinished lead — Instahyre as a labelled secondary source for Blinkit.**
Instahyre's Blinkit job pages are publicly readable and explicitly mark closed
jobs, and they link to a company listings page. The unfinished step was checking
whether that listings page yields current, verifiable openings. Nothing was
built, fetched into production, or enabled. If pursued, any rows must be
labelled as Instahyre coverage, not as Blinkit's own inventory.

Constraints that still apply to any secondary source: it can add but never
retire, so it must set `incremental=True` (see `FetchResult` in
`watcher/adapters/base.py`) and must never count as a complete sweep; and the
site currently has **no per-company coverage label** — `watcher/site/app.js`
only branches on `count === 0`, so "clearly labelled" needs export + site work
that does not exist yet. Scope that before writing an adapter.

Also noted but not implemented: user-configured native job-alert emails with
authorized mailbox ingestion. Partial discovery only, no closure signal. No
mailbox was accessed and no subscription was created.

### Microsoft: the Eightfold cap change WORKED on its first run

The 16:21 change (all nine Eightfold tenants capped; Microsoft cap 3 -> 8,
interval 4h, resume 30m) was verified at 16:54:

```
16:54:22 INFO microsoft: backoff cleared
16:54:22 INFO microsoft: sweep paused at 210 (80 fetched this run), resumes next run
```

**80 rows = 8 pages in one run, the full new cap, with no 429** — against the
2-3 requests per run it was managing before. Cursor moved **130 -> 210 of 226**.
No new 429 has occurred since the caps went in; the only 429 lines after 16:00
are repeated BACKED-OFF echoes of the 15:43 one.

**Remaining: 16 rows = 2 pages, one run.** Next resume ~17:23. If it completes,
`last_complete` jumps from 2026-09-19 00:25 (40.5h stale) to today and the
cursor clears — that is the success criterion, and it is the single most
valuable thing to check next.

### Correction to earlier blocked-source conclusions

The 16:26 section below called Citadel Securities, Gupshup and Blinkit
unautomatable. **That was wrong, and the reasoning was wrong in a specific,
repeatable way**: it concluded "no usable route" from GET-only probes that
returned 403/empty, without trying the form POST each site's own frontend uses.

- **Citadel**: the site is WordPress — its sitemap even loads
  `wp-content/plugins/wordpress-seo`. The listing is a
  `POST /wp-admin/admin-ajax.php` with `action=careers_listing_filter`, which
  returns all 84 counted rows. The signal was visible and not followed.
- **Gupshup**: the inventory route is **POST** `job/alljobs`; a GET hits a
  different handler and returns 422, which had been misread as a hard block.
- **Blinkit**: `/careers/jobs` actually returns 200 with "0 of 0 results".

**Lesson for the next session: when a page is a search UI, read the frontend
bundle for the exact request it issues — method, path and body — before
concluding a source is blocked.** A 403 on a GET is not evidence about a POST.
Flipkart/Ola (robots `Disallow: /`) and Krutrim (LinkedIn-only) remain genuine
policy blocks, not missed routes.

## Earlier handoff — 20 September, 16:53 IST — two more companies enabled

This section supersedes the 16:26 handoff below. The user asked to continue and
find any possible automation for blocked companies. After concrete live
validation and adapter tests, the user explicitly answered **“Enable both every
four hours”** for McKinsey and Citadel Securities. That authorization was acted
on; do not ask again for their activation.

**Config: 141 tracked / 133 active / eight inactive.** McKinsey and Citadel
Securities are enabled at `interval: 14400`. No changes were committed. All
pre-existing tracked/untracked changes were preserved. New runtime files are
`watcher/adapters/mckinsey.py` and `watcher/adapters/citadel.py`, registered in
`watcher/adapters/__init__.py`, with corresponding test files. **313 Python and
11 site tests pass.** No agents were spawned.

### Completed production work

1. Built and validated both adapters against live public first-party data.
2. Completed isolated temporary baselines through the real runner: 595 McKinsey
   jobs and 84 Citadel jobs, all suppressed, no errors/cursors/export/messages.
3. Received explicit activation approval.
4. Ran `/private/tmp/jw_enable_recovered_baseline.py` against the production DB
   under RunLock, using `seed=True`, `sink=None`, export disabled, honest
   `HttpClient(timeout=20, retries=0)`. Both completed at **16:50–16:51 IST**:
   595 and 84 known rows, null cursor/error/backoff, non-null last_complete,
   zero unsuppressed rows and zero notifications/export. Report:
   `/private/tmp/jw-production-recovered-baseline.json`.
5. Only after those checks, enabled both file config entries. `watcher status`
   independently confirms **McKinsey ok, 595 open** and **Citadel ok, 84 open**;
   both next due about **20:49 IST** (four hours minus scheduler slack).

Normal scheduled polls after activation are **not yet observed**. No manual
export/publication or site deployment was performed; the existing scheduler
publishes configured active companies on its normal run. Do not claim the live
site has been read back at 133 companies. The baseline processes finished.

### What changed in blocked-source conclusions

Full findings are in the new top section of `BLOCKED_SOURCE_REVIEW.md`.

- **McKinsey**: 595 unique global jobs, 46 with an India location. One configured
  match: job **110569**, Principal Software Engineer I – Social, Healthcare and
  Public Entities Tech, Bengaluru/Gurugram. The single-response adapter fails
  closed if the inventory exceeds 800; there is no mutable pagination/cursor.
  All cities/countries remain aligned and retained. The first-party gateway /
  Avature identity argument and unverified local detail-page reachability remain
  disclosed; no posting date is invented.
- **Citadel Securities — newly recovered**: public official listing and one
  detail now return 200; page 9 and a frontend bundle still sometimes 403.
  The listing publishes a public WordPress search form POST at
  `https://www.citadelsecurities.com/wp-admin/admin-ajax.php`:
  `action=careers_listing_filter`, `current_page=1`, `per_page=100`,
  `sort_order=DESC`, empty `jobTitle`, `selected-job-sections=323,325,324,326`.
  Response reports 84 found/returned and contains 84 unique title/location
  cards. Independent repeat and production fetches succeeded. Four postings
  include Gurugram; current title filter matches only **C++ Software Engineer**.
  Slug is posting identity; req_id stays blank. Some postings are evergreen
  expressions of interest. Dates are omitted. Ten-second pause honors the
  readable robots file's crawl-delay. The four category IDs come from the
  official form's default selection and may need review if its taxonomy changes.
  Count/unique/shape/location validation fails closed; >100 requires review.
- **Gupshup — readable, empty, not a current WAF-only block**: config JSON
  identifies Gupshup and reports `recruitment_enabled=false`, `allJobsCount=0`.
  The v2 JS uses **POST** `job/alljobs`, body
  `{"page":1,"limit":10,"companyId":"main"}`; it returns 200
  `{"status":"success","data":[],"job_counts":0}`. GET returns 422 because
  it hits a different handler. No positive-job parser validated, so inactive;
  config classification now `unvalidated-empty`.
- **Blinkit — readable, empty**: honest direct `/careers/jobs` returned 200,
  with “0 of 0 results” and no `.row-job-listing` rows. Published Drupal JS
  counts/filters those existing DOM rows. No positive-job schema validated;
  inactive, classified `unvalidated-empty`. This does not prove no hiring.
- **Flipkart/Ola** robots still explicitly disallow `/`; Instabase's official
  Greenhouse API still 404; Zepto TalentRecruit still 403. Krutrim's official
  CTA points to LinkedIn Jobs; Eternal remains referral-only per the existing
  first-party evidence. Do not restate permanent impossibility.

Potential additional design: user-configured native job-alert emails, with
authorized mailbox ingestion and deduplication. This avoids LinkedIn scraping
but offers **partial discovery only**, never a complete sweep or closure signal.
No mailbox was accessed, subscription created or message sent. Not implemented.

Evidence: `/private/tmp/jw-extensive-sep20/sep20-followup-*.json`, plus
`sep20-candidate-live-baseline.json`; each manifest names response body files.
Temporary baseline summary: `/private/tmp/jw-candidate-baseline-summary.json`.

### Unrelated operational follow-up still open

Readback at ~16:52: Microsoft remains at 130 (old cooldown expired, no new full
sweep); Qualcomm remains 440; Siemens progressed to 564; Nykaa still reports 32
zero polls; Quest Global still has a stale full inventory. Oracle's 46/47
count-shortfall issue is untouched. The previously approved Eightfold caps and
Microsoft timing were preserved. These were not silently marked resolved.

Next steps: observe the first normal polls for the two new adapters after
20:49; read back scheduler publication if verifying the live site; continue the
remaining operational investigations below. Do not rebaseline, commit or deploy
merely because an older section says work is pending.

## Earlier handoff — 20 September, 16:26 IST (superseded; its blocked-source verdicts are corrected above)

**Repo state:** nothing is committed (HEAD is still `ccc8057`), every tracked and
untracked change listed by `git status` must be preserved. **No background
process of any kind is running** (`ps` shows no `jw_*` or `watcher` process).
`config.yaml` validates through `watcher.config.load` at **141 tracked / 131
active / 10 inactive**. **304 Python tests + 11 site tests pass**;
`git diff --check` is clean. `config.yaml` was last edited at **16:21** (the
Eightfold caps below).

Three requests this session:

1. Continue the previous handoff's operational follow-up — **partly done**, open
   items under "Work left to do".
2. **Extensively analyse whether the ten remaining blocked companies can be
   automated** — **COMPLETE**. One is automatable (McKinsey); nine are not.
3. **Diagnose why Microsoft keeps getting 429s**, then make the two fixes the
   diagnosis implied — **diagnosis complete, both config changes made and
   explicitly approved by the user at ~16:20, but NOT yet exercised by any
   scheduled run.**

Only runtime **config** changed this session (`config.yaml`); no adapter or other
runtime code was touched, and no company was enabled or disabled.

Do not spawn agents unless the user newly authorizes it.

### Result of the extensive blocked-source analysis

**One of the ten can be automated — McKinsey, with a validated source. The other
nine cannot, each for a distinct reason with its own retrigger condition.** Full
evidence and per-company reasoning: [BLOCKED_SOURCE_REVIEW.md](BLOCKED_SOURCE_REVIEW.md),
top section. No company was enabled, no runtime code was changed, and no adapter
was written.

| Company | Reason it is blocked | Automatable now? |
| --- | --- | --- |
| McKinsey | none — counted first-party API found and validated | **Yes** |
| Flipkart | publisher preference: TurboHire robots `Disallow: /` | No |
| Ola | publisher preference: TurboHire robots `Disallow: /` | No |
| Krutrim | project policy: only destination is LinkedIn Jobs | No |
| Gupshup | Cloudflare 403 over a live Darwinbox portal | No |
| Blinkit | Cloudflare 403; parent Eternal is referral-only | No |
| Citadel Securities | WAF on details; readable sitemap has no location | No |
| Zepto | Zwayam tenant does not exist; AWS WAF on own site | No |
| Instabase | Greenhouse board removed; no job pages anywhere | No |
| Zomato / Eternal | referral-only by the employer's own design | No |

### McKinsey — validated source, NOT yet implemented

This is the one actionable recovery. The careers UI is backed by an anonymous
counted JSON search **on McKinsey's own domain**:

```
GET https://gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search?pageSize=800&start=1&lang=en
```

No key, cookie, login, Referer or special User-Agent. Reproduce with the repo's
own client (this is exactly how it was validated):

```python
import sys, json; sys.path.insert(0, '/Users/shashwattripathi/job-watch')
from watcher.http import HttpClient
from watcher import robots
url = ('https://gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search'
       '?pageSize=800&start=1&lang=en')
print(robots.allows(url))                       # -> (True, None)
body, _ = HttpClient(timeout=20, retries=0).request('GET', url)
d = json.loads(body); print(d['numFound'], len(d['docs']))   # -> 595 595
```

**What was established (two runs, 12 minutes apart, 2026-09-20 ~15:49 and ~16:01):**

- **Complete in ONE request.** `numFound` 595 = `len(docs)` 595 = 595 unique
  `jobID`. Declared equals returned equals unique, so there is no pagination
  boundary to defend in the normal case.
- **India cross-validated two independent ways.** `&countries=India` returns
  `numFound` 46 / 46 docs, and that ID set is **exactly** the 46 derived
  independently from the global response's per-record `countries[]` arrays —
  no missing rows, no extras, in both runs.
- **Stable.** The repeat run returned the identical 595 IDs **in identical
  order**, and India 46 both times.
- **Locations are derivable per job.** `cities[]`, `countries[]` and
  `continents[]` are equal-length and positionally aligned on **all 595**
  records, so India cities come from
  `[c for c, co in zip(d['cities'], d['countries']) if co.strip().lower() == 'india']`.
  Every India record yields at least one India city. Distribution: Gurugram 40,
  Chennai 22, Bengaluru 17, Mumbai 7, Kolkata 4. Forty are India-only postings;
  six are multi-country postings that include India — the same multi-location
  case Siemens and Uber already handle.
- **Fields present on 595/595:** `jobID`, `title`, `cities`, `countries`,
  `friendlyURL`, `jobApplyURL`. (`functions` 365/595, `industries` 352/595,
  `shortJobSummary` 50/595 — all optional.)
- **Pagination semantics:** `pageSize` plus a **ONE-based page index** `start`.
  Verified: `start=2&pageSize=10` returned exactly global rows 11–20. `rows=`
  is **not** a valid parameter and returns HTTP 400.
- **Robots.** `www.mckinsey.com/robots.txt` is served and allows `/careers/...`
  (its `Disallow: /search` is a different path prefix, not a match).
  `gateway.mckinsey.com/robots.txt` is itself a **Cloudflare 403**, so no rules
  are delivered; `watcher.robots.fetch` maps every HTTP error to an empty
  ruleset, which is what RFC 9309 specifies for an unavailable-status robots
  file and is the same situation already accepted for Uber (404). The API itself
  answers a clean `200 application/json` with no challenge header.

**Three honest gaps — verification work, not access barriers:**

1. **Public detail pages are unreachable, so identity has no detail-page
   confirmation.** `www.mckinsey.com/careers/search-jobs` and two constructed
   `/careers/search-jobs/jobs/<friendlyURL>` details fail at the **TCP level**
   (`[Errno 60] Operation timed out`, ~18s) even with a 75-second timeout, while
   `robots.txt` on that same host succeeds. Identity therefore rests on: the
   data being served from the first-party host `gateway.mckinsey.com`, every
   `jobApplyURL` pointing at McKinsey's own Avature tenant
   (`mckinsey.avature.net/careers/ApplicationMethods?folderId=<jobID>`), and
   McKinsey's own careers taxonomy in `interestCategory` / `jobSkillGroup`.
   That is first-party attribution rather than an ATS tenant guess, but it is
   weaker than the detail-page confirmation used for LinkedIn, Uber and Siemens.
   **Flag this to the user rather than quietly treating it as equivalent.**
2. **No reliable posting date.** The only date-like field is
   `postedToLinkedInDate`, on 483/595. `posted_at` is a free-text TEXT column so
   an empty value stores fine, but there is no "new since" date.
3. **`apigw-x0cceuow60` looks like a deployment identifier** and could rotate —
   the same fragility already documented for Meta's persisted query IDs.

**Implementation plan if the user approves** (they were asked and had not yet
answered when this handoff was written — **do not enable anything without that
go-ahead**, since it adds recurring third-party traffic):

1. Write `watcher/adapters/mckinsey.py` modelled **directly** on
   `watcher/adapters/bain.py` — Bain is the same single-oversized-request
   pattern (326 rows in one 400-row response) and already solves the cursor,
   `page_size` validation and `RobotsAware` plumbing. Differences to handle:
   `start` is **one-based** (Bain's is zero-based), the count field is
   `numFound`, rows are under `docs`, and locations come from the aligned
   `cities`/`countries` zip joined with `join_locations`.
2. Guards it must enforce, matching house style: `numFound == len(docs)` and
   unique `jobID` before reporting `complete=True`; fail closed (raise
   `AdapterError`) on unexpected response shape so a rotated base path cannot
   silently yield zero rows; reject a cursor whose accumulated count disagrees
   with `page * page_size`.
3. Add `tests/test_mckinsey.py` and register the adapter in
   `watcher/adapters/__init__.py`.
4. Run a **silent production baseline** under RunLock with notifications and
   export disabled — the same sequence used for LinkedIn, Uber and Siemens
   today — and only then change `config.yaml` from `ats: unsupported`.
5. `config.yaml` already records the endpoint and all of the above in the
   `mckinsey:` entry's comment. It was deliberately **not** added to `recheck:`,
   so no new traffic exists today.

### The nine blocked companies — new evidence this session

Details in [BLOCKED_SOURCE_REVIEW.md](BLOCKED_SOURCE_REVIEW.md). What is new or
corrected, so it is not re-investigated from scratch:

- **Zepto is decided at the platform level.** Zwayam's own robots-allowed
  configuration API rejects **both** documented hostnames:
  `POST https://public.zwayam.com/data-service/v2/public-configurations`
  (multipart `companyUrl`) returns `403 {"message":"Invalid CompanyUrl..."}` for
  `zeptonow.openings.co` **and** `zeptonow.cluster3.openings.co`. That is the
  platform saying the tenant does not exist — not bot protection. The `cluster3`
  shell is a Next.js static export whose `__NEXT_DATA__` has empty `pageProps`
  and contains no company identifier, so no mapping can be read from it. Its
  `TechCorp Careers` label and Flipkart/preprod fallbacks are defaults and must
  never be used as identity.
- **Citadel has readable data that still cannot be used.** New: the sitemap
  index exposes `career-sitemap.xml`, which returns **200 with 88
  `/careers/details/<slug>/` URLs** — but `robots.txt` and
  `/careers/open-opportunities/` both return 403 (WAF), so details cannot be
  read. The sitemap has **no job IDs, no titles beyond the slug, and no
  locations**; all 88 `lastmod` values are the same bulk regeneration
  (2026-09-20 09:22Z) so they are not per-posting signals; and **zero slugs name
  any India city**, while several are evergreen campus-referral pages with
  `-asia` / `-us` suffixes. Since this watcher filters to India, an inventory
  with no location field cannot be filtered honestly.
- **Instabase is conclusive, not obstructed.** Its careers page and sitemap are
  both fully readable. The page's JS still hard-codes Greenhouse board
  `instabase` (`boards-api.greenhouse.io/v1/boards/instabase`, details
  `8042464002` and `8073772002`), all of which 404. The sitemap contains exactly
  three careers URLs (`/careers`, `/careers/jobs`,
  `/careers/jobs/recruitment-fraud-alert`) and **no individual job pages at
  all**, and the page references no other ATS.
- **Gupshup has NO robots rules — correcting an earlier note.**
  `gupshup.darwinbox.in/robots.txt` returning "200" is **not a robots file**: it
  is the SPA's HTML shell. So Gupshup is purely a Cloudflare WAF block and was
  never a policy block. Its bundles identify the exact read-only routes
  (`getCompanyConfig`, `job/alljobs`, `job/filters`, legacy `companyinfo`,
  `job?page=1`); every one 403s. Official `gupshup.ai/careers` CTAs route
  candidates to WhatsApp.
- **Flipkart and Ola are publisher preference, not failure.** Re-confirmed with
  the repo's own matcher: `flipkart.turbohire.co/robots.txt` and
  `olacareers.turbohire.co/robots.txt` are both **served 200** with
  `User-agent: * / Disallow: /`, and `Robots.allowed()` returns False with
  `why → "Disallow: /"`. The boards are reachable and deliberately not polled.
  No board path was fetched. Flipkart's permitted first-party `/jobslist` is
  still a static ten-card mock with a hard-coded 69-opening heading and a Show
  More that only alerts.
- **Krutrim** — the AI Labs bundle opens `linkedin.com/company/krutrim/jobs/`
  and nothing else; `/careers` still 404s. Ola's shared TurboHire org does not
  establish Krutrim membership.
- **Blinkit** — robots-permitted `blinkit.com/careers/jobs` returns a branded
  Cloudflare 403 with no job API lead; `jobs.blinkit.com` also 403s and
  identifies as a Vendor Dashboard. Do not infer zero jobs from a 403.
- **Zomato / Eternal** — the careers component accepts applications only through
  employee referrals and makes no listing request. SmartRecruiters `Zomato1` is
  real but abandoned (2015–2016 rows; one still accepts applications, which does
  not make it a current inventory).

**Do not describe any of these nine as permanently impossible.** Each has a
retrigger recorded in BLOCKED_SOURCE_REVIEW.md, and three (Gupshup, Blinkit,
Citadel) are live portals whose data exists and is merely unreadable by an
honest client.

### Live operational state, read from `watcher status` at 16:07 IST

Accurate as of that read; re-read rather than trusting these numbers later.

- **Microsoft — diagnosed and reconfigured at 16:21; NOT yet observed working.**
  Progressed 60 → 90 → 110 → **start=130 of 226**, then took another HTTP 429 at
  15:43. Root cause is two-part: (a) Eightfold throttles by **volume from our
  IP, not spacing** (a 23-request sweep tripped 429 at both 0.5s and 6.0s), and
  Microsoft was the **only** tenant with a page cap while seven of nine were
  uncapped — Qualcomm pulled **440 rows / 44 requests in ONE run**, twice, about
  15x Microsoft's whole hourly budget; (b) 23 pages at 3/run is ~8h of clean
  runs, and `eightfold.py` restarts a sweep from zero whenever the declared
  count changes or a page overlaps known IDs, which an 8h window on a live board
  cannot avoid (count drifted 239 → 226; `last_complete` was 39h stale). A 429
  is the cheap failure — it keeps its cursor; a drift restart is the expensive
  one. Shared per-IP quota across tenants remains a **hypothesis**, unconfirmed
  by response headers. See "Eightfold volume note" in `config.yaml` under
  `microsoft:` for the full reasoning.
- **Oracle (the company `oracle`) is erroring every hour:**
  `AdapterError: oracle: incomplete Oracle page at 46/47`. Diagnosed this
  session as **upstream inconsistency, not a parser bug**: the API declares 47,
  returns 46 unique rows at offset 0, then zero at offset 46; no-sort,
  bare-expand and the India location-facet query all produce the same 46 IDs and
  the same declared 47. Completeness guards were deliberately retained, so the
  company simply never completes. **This needs a user decision** — tolerate a
  small declared-count shortfall for this source, or leave it erroring.
- **Quest Global — `STARVING: no complete sweep in 22.8h, 1001 open`.** Unique
  coverage previously measured 962/1000. Unresolved.
- **Nykaa — `0 rows for 32 poll(s) in a row`.** Newly noticed in this status
  read and **not yet investigated at all**. Its `skima` adapter is an HTML
  adapter, and the house rule is that zero rows from an HTML adapter is a
  warning state, not a quiet week. Worth a look.
- **Qualcomm — the `cursor_max_age: 86400` change is working:** `sweep in
  progress (at 440)`, i.e. the 440-row partial sweep survived instead of being
  discarded at the old three-hour expiry. A *completed* sweep has still not been
  observed (31.6h as of 16:20). Last complete was 19 September 08:45. Now also
  capped at `max_pages_per_run: 20`, so its 58-page sweep takes three runs
  instead of two; it has no `resume_interval`, so it resumes every run.
- **Siemens — `sweep in progress (at 468)`** of 939. Normal sweeps are still
  running long; last full completion was the 11:28:56 baseline.
- Healthy recoveries confirmed still polling normally: **LinkedIn 57 open**,
  **Uber 20**, **Meta 24**, **Bain 327**, **Navi 41**, **Akamai 18**,
  **GreyOrange 88**, **BookMyShow 13**, **Caterpillar 99**, **HSBC 147**.
- Live published snapshot was verified earlier today: `generated_at=1789898297`,
  **131 companies / 2,253 jobs**, LinkedIn 22, Siemens 82, zero inactive
  entries. Publication is reconciled — **no republish or deployment is needed**,
  and none should be run unless a readback shows it is stale.

### Eightfold config change made at 16:21 — all nine tenants now capped

Every Eightfold tenant now has a `max_pages_per_run`, so the largest possible
single-run burst is **20 requests, down from the 44 observed**. Caps were sized
so each tenant's own full sweep still fits in ONE run, to avoid creating new
starvation; Qualcomm is the exception because 58 pages physically cannot.

| tenant | interval | resume | cap | sweep pages |
| --- | --- | --- | --- | --- |
| netflix | default | — | 4 | 1 |
| eightfoldai | 1h | — | 6 | 3 |
| microsoft | **4h** | **30m** | **8** | 23 |
| morganstanley | 1h | — | 14 | 11 |
| qualcomm | 1h | — | **20** | 58 |
| netapp | 2h | — | 10 | 8 |
| lamresearch | 1h | — | 14 | 12 |
| ukg | 1h | — | 10 | 8 |
| hsbc | 3h | — | 20 (unchanged) | 17 |

Microsoft also moved from hourly to a **4h sweep start** with a **30-minute
resume while a sweep is in progress**, and its cap went 3 → 8. Rationale: fewer
sweep starts (6/day instead of 24 that never complete), but a much shorter
wall-clock window per sweep (~1.5h instead of ~8h) so it can outrun the drift
guard. Backoff still gates resumes, so a 429 costs `backoff_min` regardless.
The cap was deliberately left at 8 rather than 12+: the observed quota was only
2-3 requests per run, so this change **depends on the other tenants' caps
actually freeing shared volume**. That is the thing to verify.

**Not yet exercised live.** `config.yaml` was saved at 16:21 and the preceding
scheduled run was 16:18, so no run has used these settings. 304 Python + 11 site
tests pass and the config loads at 141/131/10.

### Work left to do

Roughly in priority order:

1. **McKinsey: the user was asked at ~16:07 and had not answered** when this was
   written (they moved on to the Microsoft 429 question). On a yes, implement per
   the five-step plan above. This is the only new automation available.
2. **Decide the Oracle 46/47 policy with the user.** It errors hourly right now.
   Options: allow a bounded declared-count shortfall for this source, or accept
   the permanent error state. Do not weaken the shared completeness guard for
   every Oracle company to fix one.
3. **Observe a Microsoft full sweep actually complete** (226 India rows) under
   the 16:21 config change — this is the open verification. It has never been
   seen end-to-end. Microsoft was backed off until 16:43 and the first run under
   the new settings is the ~16:33 process at the earliest, so the earliest
   plausible completion is roughly 18:00 (3 runs at 30-minute resumes). Check
   with `watcher status`: success is **microsoft with no cursor and a
   `last_complete` inside the last ~6h**. If it still starves, the next lever is
   **fewer rows to page** (an Eightfold `query` to narrow the 226), NOT more
   pages per run — the observed quota was only 2-3 requests per run, so raising
   the cap further would just convert clean pauses into 429s.
4. **Investigate Nykaa's 32 consecutive zero-row polls.** Untouched.
5. **Confirm the newly capped Eightfold tenants still complete their sweeps.**
   The caps were sized above each tenant's page count precisely so this stays
   true, but it is unverified: watch netflix, eightfoldai, morganstanley, netapp,
   lamresearch and ukg for any `sweep paused` line, which would mean a cap is
   too tight and is creating a second Microsoft-style starvation. Also observe
   one complete **Qualcomm** sweep (58 pages, now three runs at cap 20) and one
   complete **Siemens** normal sweep.
6. **Quest Global unique coverage 962/1000** — still unexplained.
7. Nothing is committed. If the user wants this session's work committed, the
   diff spans `config.yaml`, the three top-level audit/handoff docs, and the
   large set of untracked adapters/tests from earlier sessions.

### Ground rules that were followed — keep following them

Direct checks used `watcher.http.HttpClient` with the honest
`job-watch/0.1.0 (personal job-alert poller)` User-Agent, 20-second timeouts,
no retries, and 1.2-second pacing. `watcher.robots` was consulted before every
target request and on every redirect. There was **no** challenge solving,
browser impersonation, proxy, alternate-host bypass, authentication, job
application, notification, production baseline, or deployment. Search results
were used for **discovery only**, never as a job inventory. Missing or
error-status robots files were not invented into prohibitions, and a served
`Disallow` was never overridden. Failures were not retried in tight loops.

### Evidence

Volatile diagnostics — **the findings above are the durable record**:

- `/private/tmp/jw-extensive-sep20/` — response bodies and per-company JSON
  manifests for this session's probes (`mckinsey-coverage`, `mckinsey-repeat`,
  `mckinsey-detail`, `citadel-sep20`, `citadel-careersitemap`,
  `instabase-sep20`, `zepto-sep20`).
- `/private/tmp/jw_extensive.py` — the probe helper (robots-gated, paced).
- `/private/tmp/jw-mckinsey-{gateway,pagination,search-html,coverage}.log`.
- `/private/tmp/jw-oracle-afternoon/`, `/private/tmp/jw-oracle-{afternoon,compare}.log`
  — the Oracle 46/47 diagnosis.
- `/private/tmp/jw-sep20-published-verified.json` — live KV verification.
- Earlier LinkedIn/Siemens/Uber evidence paths are listed in the older sections
  below.

## Earlier handoff — 20 September, 11:50 IST (superseded by the START HERE section above)

**This section supersedes all older current-state claims below.** User asked
to try automating the remaining companies, then interrupted to prioritize this
handoff because usage was nearly exhausted. **LinkedIn is now recovered through
SmartRecruiters, silently baselined, and enabled.** There are **141 tracked,
131 active, 10 inactive**. Preserve every tracked/untracked edit; nothing was
committed. Do not spawn agents unless newly authorized.

### LinkedIn recovery completed

- Earlier claims that LinkedIn as an employer has no permitted alternative
  were too strong. Found `https://careers.smartrecruiters.com/LinkedIn3`, with
  employer identity independently confirmed by public detail responses. This
  does **not** scrape the LinkedIn Jobs platform; that restriction remains.
- The separate `api.smartrecruiters.com` robots explicitly disallows crawling;
  no postings API request was sent there. The permitted career-page HTML and
  its own published `/LinkedIn3/api/more` route work with the honest client.
  Career-page robots redirects to a generic HTML page with no disallow rule;
  public job-detail host robots returns 404.
- Enumerated all six global location-group pages to establish India coverage:
  **Bengaluru 51, Gurgaon 2, Mumbai 4**. `?search=India` returns precisely those
  three groups. Its initial group cards contain nine rows, then ten per Show
  more request (final Bengaluru fragment has two). Complete scan: **57 unique
  jobs, 22 title/location matches, seven inventory requests**.
- Detail samples from all three cities confirmed exact employer, title,
  location, public application URL and available application state. Sample
  posting dates: Bengaluru 18 September, Gurgaon 17 September, Mumbai
  15 September 2026. No application was submitted.
- Added `watcher/adapters/smartrecruiters_html.py`, registered it in
  `watcher/adapters/__init__.py`, and added ten tests in
  `tests/test_smartrecruiters_html.py`. Adapter validates employer, complete
  group pagination, visible/attribute counts, required identity/title/location,
  exact per-page coverage, safe URLs and duplicate IDs. It rereads the starting
  index and requires matching counts/group heads before completing. All rows
  stay in its resumable cursor until verification; throttle retains the exact
  pending page, changed count/head or overlap restarts without rows. Empty
  inventory requires explicit empty-state text on two reads.
- Real-runner silent production baseline completed **11:47:05 IST** under
  RunLock: **57 stored / last_count=57**, last_complete=1789885015 (11:46:55
  sweep start), cursor null, no error/backoff, `sent=0`, `exported=null`, all
  notifications suppressed (`notified=2`). Only then changed `config.yaml` from
  blocked to `smartrecruiters_html`, token `LinkedIn3`, organization `LinkedIn`,
  country `India`, strict location `[india]`, hourly interval, 1.2-second pacing,
  12 requests/run, three-hour cursor lifetime.
- Updated `README.md` to clarify the restriction is LinkedIn-platform scraping,
  and `watcher/adapters/README.md` with source/baseline details. Its first normal
  scheduled poll has **not** been observed; earliest due **12:46:55 IST**.
- Evidence: `/private/tmp/jw-linkedin-validation.json`,
  `/private/tmp/jw-linkedin-baseline.{log,json}` and manifests/bodies named
  `linkedin-*` inside `/private/tmp/jw-remaining-sep20/`.
  Baseline script `/private/tmp/jw_linkedin_baseline.py` has completed; do not
  rerun it (it asserts no prior baseline).

### Publication was started; reconcile before publishing again

- Started existing `/private/tmp/jw_publish_current.py` after enablement using
  RunLock and the existing Cloudflare credentials. Exec session **34823** was
  returned, then the user interrupted before its final output was collected.
- At 11:50, no process named `jw_publish_current.py` remained. Local exported
  `docs/data/jobs.json` has **131 companies, watched=131, 2,258 jobs**, including
  LinkedIn's **22**, generated_at=1789885085. This proves local export, **not
  successful live KV publication**. No new asset changes were made.
- Next: collect session 34823 if available, then run the read-only
  `/private/tmp/jw_verify_active_snapshot.py` to verify live KV. It verifies the
  active-company set and Siemens's 82 matches, and writes
  `/private/tmp/jw-sep20-published-verified.json`; also verify the LinkedIn shard
  contains 22 jobs. Do not assume an older verification file proves this update.
  If already published, avoid another deployment. Retry the existing publisher
  only if readback shows publication failed/stale. Never print env credentials.

### Remaining-company investigation this continuation

Fresh evidence is in `/private/tmp/jw-remaining-sep20/`, summary log
`/private/tmp/jw-remaining-sep20.log`, helper `/private/tmp/jw_remaining_sep20.py`.
Search results were discovery leads only, not a pollable current inventory.
No new source other than LinkedIn was enabled. **Do not claim the remaining
companies can never be automated.**

| Company | New/reconfirmed finding and remaining lead |
| --- | --- |
| Zepto | Historical `zeptonow.openings.co` returns 404. Discovered `zeptonow.cluster3.openings.co` responds with a new Zwayam Next.js frontend. Its linked JS establishes `POST https://public.zwayam.com/data-service/v2/public-configurations` with multipart `companyUrl`. Exact request for that host returned 403; gzip-decoded body says `Invalid CompanyUrlzeptonow.cluster3.openings.co`. The shell's `TechCorp Careers` label and Flipkart/preprod fallback values are defaults, **not employer identity**; do not use fallback company IDs or activate them. The linked TalentRecruit robots request again hit the old Python TLS error; earlier modern-Python 403 evidence still stands. A valid current Zwayam mapping is a concrete unresolved lead. |
| Gupshup | Legacy Darwinbox shell remains readable. Its own JS exposed additional exact legacy GET routes `/ms/candidateapi/companyinfo` and `/ms/candidateapi/job?page=1`; both returned 403. This supplements earlier v2 API failures; it is not proof the company has no jobs. |
| Instabase | A recently indexed third-party page linked newer Greenhouse ID 8783218002. Direct API still 404; direct `job-boards.greenhouse.io/instabase` and that newer detail both returned 404 at 11:48. The interrupted final probe actually finished and saved `instabase-current.json`. Do not import cached search jobs as current inventory. |
| Citadel Securities | Direct current `/careers/open-opportunities/` still 403. Web-index rendering shows 84 results and proper location groups, but no new direct usable endpoint was established. Prior readable sitemap and blocked REST evidence remain relevant. |
| McKinsey | Fresh honest direct search request again timed out at 20 seconds. Earlier reachable candidate portal still provides no verified listing; no alternate complete feed established. |
| Flipkart | Fresh official homepage still links the same TurboHire board. Fresh robots confirms default `Disallow: /`; Googlebot exceptions do not apply. No board path fetched. Earlier first-party jobslist remains an unvalidated placeholder. |
| Ola | Fresh Electric careers HTML still links the same TurboHire org. Fresh board robots disallows the watcher. No board path fetched; no new first-party inventory established. |
| Krutrim | Fresh AI Labs page still exposes the same landing-page bundle previously proven to link LinkedIn. No new independent inventory found. No inference that Ola's board covers Krutrim. |
| Zomato | Fresh Eternal careers page still loads; existing referral-only component evidence remains, with no new enumerable feed found. |
| Blinkit | Fresh official jobs route returned 403 with a large branded error page (`blinkit | Error Page`, canonical `/careers/error-page`), no job data. Do not infer zero jobs or label it a specific challenge without evidence. |

### Newly observed production issue and remaining monitoring checks

- **Oracle's own tenant needs follow-up.** At **11:34:00 IST**, the normal
  scheduler with the hardened parser recorded
  `AdapterError: oracle: incomplete Oracle page at 46/47`. Last complete count
  remains 46, cursor null, no blocked_until. This is a fail-closed completeness
  error, not a proven empty/new complete inventory. Investigate the actual
  declared-count/page discrepancy without relaxing completeness guards or
  forcing early polls. No Oracle fix was attempted in this continuation.
- The other existing Oracle tenants **did complete normal polls after the
  hardened parser** around 11:33–11:34, with no cursor/error/backoff: JPMorgan
  323, American Express 52, BNY 181, Akamai 18, Texas Instruments 107, Honeywell
  328. This supersedes the prior handoff's pending normal-use verification for
  those tenants.
- Microsoft remains at **60/226**, no error/backoff, last_polled 11:11:22;
  next eligible **12:11:22 IST**. Normal resume-priority scheduling and eventual
  full completion remain unverified. Last complete count is still 239.
- Uber remains baselined at 20 / 3 matches; first normal poll due after
  **12:10:15 IST** remains unverified. Siemens remains enabled with its validated
  939-job baseline / 82 matches; no new complete normal sweep observed.

### Verification and exact next steps

- **304 Python tests and 11 JS tests pass**, logs
  `/private/tmp/jw-remaining-tests.log` and `/private/tmp/jw-remaining-js.log`.
  Latest adapter checks were included. `git diff --check` passed before the
  final config/docs edits; rerun it after this handoff. No UI code changed and
  no browser check was performed this continuation.
- `AUTOMATION_AUDIT.md` and `BLOCKED_SOURCE_REVIEW.md` still have their earlier
  130-company latest sections; **update them from this handoff next**. The user
  explicitly prioritized saving this file before usage runs out.
- First reconcile the pending publication and verify LinkedIn's live shard.
  Then investigate the Oracle count discrepancy and observe normally due
  Microsoft/Uber/LinkedIn polls. Continue remaining-company research from
  concrete permitted leads, preserving strict coverage and silent baselines.
- No named baseline/publish/recovery script was still running at the 11:50
  process check. Interrupted tool commands may have partially executed; verify
  specific process commands before starting replacements. Never kill launchd.

## Earlier handoff — 20 September, 11:31 IST (superseded)

**This section supersedes older current-state claims below. Siemens is now
enabled and published. There is no running Siemens diagnostic.** Preserve all
tracked/untracked changes; nothing has been committed.

### Siemens baseline completed, validated, enabled, and published

- Inspected existing PID 47327 and confirmed its command before taking any
  action. Let that same bounded process finish; no duplicate baseline or extra
  source requests were started. It completed at **11:28:56 IST** and exited.
- Real-runner result: `ok=[["siemens",939,62,0]]`, no partial/failure/backoff,
  `sent=0`, `exported=null`, cursor null. Database has **939 unique jobs**,
  last_count=939, last_complete=1789882189 (the sweep's 10:59:49 start), and no
  error/backoff. All 939 records have validated title/location/identity/URL,
  were seen in this sweep, and have notifications suppressed (`notified=2`).
- **82 jobs match** current title/location filters. Three retained facet
  anomalies (493275, 516009, 517911) remain correctly excluded by location.
- Only after verification, enabled Siemens in `config.yaml` and documented
  its adapter/baseline in `watcher/adapters/README.md`. Regular scheduled polls
  now apply; a new normal Siemens sweep has not yet been observed.
- Published under the existing RunLock using the established Cloudflare data
  publisher: `snapshot updated (751 KiB); code unchanged`. Read back the actual
  Cloudflare KV snapshot and verified **130 companies, watched=130, 2,236
  matching jobs, Siemens=82, 18 empty companies, zero inactive companies**.
  Snapshot generated_at=1789884004. No asset deployment was needed.
- Evidence: `/private/tmp/jw-siemens-baseline-sep20-resume.{log,json}`,
  `/private/tmp/jw-siemens-completion-verified.json`, and
  `/private/tmp/jw-sep20-published-verified.json`. Verification scripts are
  `/private/tmp/jw_verify_siemens_complete.py` and
  `/private/tmp/jw_verify_active_snapshot.py`. Temp files may be removed later.

### Remaining time-dependent verification

- **Microsoft remains incomplete**, at 60/226, last_polled **11:11:22 IST**,
  no error/backoff; last_complete is still the 239-job inventory with sweep
  start 19 September 00:25:46. Earliest resume **12:11:22 IST**, subject to
  launchd cadence and run budget. Do not force an early request or call it fixed.
  Observe whether its first normally due priority resume uses the next existing
  Eightfold slot, then track a coherent complete sweep and any restart reason.
- **Uber remains healthy at its silent baseline of 20 jobs / 3 matches**,
  last_polled 11:10:15. Observe normal polling after **12:10:15 IST**; this
  continuation did not send a new Uber request.
- Existing Oracle tenants were read-only checked: Oracle 46, JPMorgan 323,
  American Express 52, BNY 181, Akamai 18, Texas Instruments 107, Honeywell 328;
  all cursor-free with no error/backoff, but their last polls were around 10:29,
  before the hardened Oracle parser. Its normal production use remains to verify.
- Scheduler lock contention at 11:03 and 11:18 occurred while the Siemens
  baseline owned the lock. Its process has now exited; scheduler was never killed.
- There are **141 tracked / 130 active / 11 inactive**. Inactive: LinkedIn,
  Gupshup, Krutrim, Flipkart, Zepto, Zomato, Blinkit, Instabase, Citadel Securities,
  McKinsey, Ola. Continue recovery only from concrete permitted leads.
- Verification this continuation: **294 Python tests, 11 JS tests pass;
  git diff --check clean**. Logs:
  `/private/tmp/jw-sep20-continuation-tests.log` and
  `/private/tmp/jw-sep20-continuation-js.log`. No runtime logic or UI changes
  were needed. No live signed-in browser check was performed.

## Earlier handoff — 20 September, 11:18 IST (superseded)

**This section supersedes all older current-state claims below.** The user asked
to preserve the latest work and remaining tasks before context runs low.
There are **141 tracked companies: 129 active, 12 inactive**. Uber is now
recovered and enabled. Microsoft has progressed but has not completed a new
sweep. **A bounded Siemens baseline is still running: inspect it before doing
anything that could duplicate it.** Nothing was committed; preserve the dirty
tracked and untracked work.

### Running process — inspect first next session

- At 11:17:34 IST, **PID 47327** was running
  `/private/tmp/jw_siemens_resume_sep20.py` with the repository `.venv-dev`
  Python. Verify PID and command again; PIDs can be reused.
- Log: `/private/tmp/jw-siemens-baseline-sep20-resume.log`.
  Structured latest state: `/private/tmp/jw-siemens-baseline-sep20-resume.json`.
- It resumes the existing coherent sweep, holds the production `RunLock` only
  for each bounded chunk, releases it between chunks, and waits if another
  process owns it. Sixteen requests maximum per chunk, existing 1.2-second
  jittered spacing, 20-second HTTP timeout, no retries. It stops on restart,
  error, throttle, offline, completion, or a roughly 20-minute bound from its
  11:12 start. A current chunk can finish after that time bound.
- It uses `seed=True`, `sink=None`, and export disabled. It **does not enable
  Siemens or publish anything**, even if it completes. No diagnostic sends
  notifications. Do not kill the normal launchd scheduler or start a duplicate
  baseline. No other diagnostic process was running at this snapshot.

### Completed: Uber recovery and live publication

- Validated the anonymous Oracle candidate site at
  `https://iaziqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1`.
  It identifies itself as **Uber Careers**, links official Uber properties,
  and serves public requisition search/details without authentication.
- Enumerated **531 unique global jobs over three pages** with stable declared
  counts. Their IDs exactly match the previously fetched official
  `https://jobs.uber.com/en/jobs/sitemap.xml` (531 IDs; zero missing or extra).
  The India query returns **20 jobs**, exactly matching all primary/secondary
  India locations in the complete global inventory. No secondary-only India
  row was present in this sample. Two public details confirmed ID, title,
  location, and Uber description. Robots returned 404, with no disallow rule.
- Fixed `watcher/adapters/oracle.py` to recognize secondaryLocations[].Name
  alongside LocationName/Location. Filter diagnostics now consider secondary
  locations too. Added strict declared count, unique IDs, page coverage,
  required identity/title/location, and per-location resume validation.
  Count drift/overlap returns restart with zero rows; 403/429 retains paid-for
  rows and exact offset. Old cursors without loc_ids restart without HTTP.
  These safeguards also affect existing Oracle tenants; their next legacy
  partial sweep may safely restart.
- Added `tests/test_oracle_completeness.py` (eight tests covering those
  behaviors). Existing four Oracle fixture tests also pass.
- First baseline attempt stopped before any HTTP because connectivity briefly
  vanished. After connectivity returned, the **11:10:15–11:10:16 IST** silent
  production baseline completed: **20 known / 20 declared**, cursor null,
  last_complete=1789882815, no error/backoff, zero notifications/export.
- Only after that success, changed `config.yaml` Uber from blocked to Oracle,
  host `iaziqy.fa.ocs.oraclecloud.com`, site `CX_1`, locations `[India]`, interval
  3600. **Three jobs pass the current title/location filters.** Normal polling
  resumes after its due time; baseline backlog was suppressed silently.
- Published the updated active-company snapshot under RunLock using existing
  Cloudflare deployment credentials without displaying them. Result:
  `snapshot updated (728 KiB); code unchanged`. Export now has **129 companies,
  watched=129, 2,154 matching jobs**, generated_at=1789883150. The earlier
  zero-match-company UI fix remains deployed; no new asset deployment needed.
  The export excludes disabled Siemens partial data.
- Evidence: `/private/tmp/jw-uber-validate.log`,
  `/private/tmp/jw-uber-coverage.log`, `/private/tmp/jw-uber-global.json`,
  `/private/tmp/jw-uber-baseline-retry.log`, `/private/tmp/jw-uber-baseline.json`,
  `/private/tmp/jw-publish-sep20.log`, and matching manifests/bodies in
  `/private/tmp/jw-recovery-review/`. Temp files may eventually be removed.

### Microsoft: new scheduling fix, live progress, remaining proof

- At **11:11:22–11:11:36 IST**, a normally due, locked, silent runner call
  successfully fetched offsets **30, 40, 50**, with no 429, count change, or
  overlap. Cursor advanced from 30 to **60 of 226**, with 60 unique ids and
  loc_ids. Current sweep started at 10:11:10 (1789879270); last_polled is
  1789882882. No backoff/error. Earliest next resume: **12:11:22 IST**.
- Last complete inventory remains **239**, sweep-start epoch 1789757746
  (19 September 00:25:46; completion was logged at 01:54). **Microsoft is not
  yet proven fully recovered.** Do not equate successful chunks with complete
  closure detection or permanently eliminated 429s.
- Found and addressed the scheduling bottleneck: pure oldest-first rotation
  across nine tenants deferred a due Microsoft partial sweep for several
  hours. Added `resume_priority: true` for Microsoft and
  `runner._prioritize_resumes()`. It reorders only entries within the existing
  capped ATS slots, placing opted-in **due** cursors first. A positive
  resume_interval is required. Other ATS positions are untouched; other
  Eightfold tenants retain oldest-first order in intervening slots.
- This keeps **max_parallel=1, max_per_run=1, max_pages_per_run=3,
  resume_interval=3600, backoff and Retry-After** intact. It creates no extra
  Eightfold slot and never polls early. Peers rotate more slowly while
  Microsoft has a due cursor; this is the deliberate scheduling tradeoff.
  Seed runs retain their previous behavior.
- Three new runner tests prove an hourly priority resume over sixteen
  simulated 15-minute runs while all eight peers get turns; enforce the
  one-slot cap and backoff; and preserve unrelated ATS positions. A read-only
  reconstruction with production state places Microsoft first at its next
  eligibility without moving other ATS slots. **The next actual scheduled
  priority resume has not happened yet.**
- Inspected Microsoft's current public shell and its linked application JS.
  It uses the same `/api/pcsx/search` endpoint and sort_by parameter. Captured
  responses echo timestamp sort, but a small timestamp inversion still occurs;
  do not re-enable unsafe incremental early stopping. No alternate complete
  feed was established and no additional search request was forced early.
- Evidence: `/private/tmp/jw-ms-due-sep20.log`,
  `/private/tmp/jw-ms-response-{1,2,3}.json`,
  `/private/tmp/jw-resume-priority-tests.log`. Shell/bundle bodies and manifests
  are in `/private/tmp/jw-recovery-review/`.

### Siemens current state — incomplete and still disabled

- Started a fresh sorted sweep at **10:59:49 IST** (1789882189), discarding only
  the intentionally interrupted previous cursor, not deleting stored jobs.
  This fresh sweep declares **939**, superseding yesterday's 953 count.
- A network outage at 11:04 stopped it safely at 223 IDs/offset 228/pending 5.
  After the network recovered, resumed that cursor; no count drift/overlap has
  occurred so far. The 11:17 state has **432 known IDs, cursor offset=438,
  pending=6, total=939, order=postedDate:ASC**, last_complete=None, no error or
  backoff. These are a timestamped snapshot; the process above is continuing.
- Original attempt logs/report:
  `/private/tmp/jw-siemens-baseline-sep20.{log,json}`. Resume files are listed
  above. One RunLock contention during publishing was handled by waiting.
- Once the process finishes, require a successful real-runner completion,
  cursor null, last_complete set, coherent current count/unique IDs, correct
  title/location/URL fields, no error/backoff, and zero notifications/export.
  Only then enable Siemens and publish the newly active company's snapshot.
  If it restarts or fails, leave disabled and diagnose the exact cause; do not
  repeatedly restart/hammer the full board or weaken completeness checks.

### Verification, other findings, and next-session order

- Latest full suite after Oracle/scheduling changes: **294 Python tests pass**
  (`/private/tmp/jw-sep20-final-tests.log`), **11 JS tests pass**
  (`/private/tmp/jw-sep20-final-js.log`); `git diff --check` clean at 11:17.
  The **26 local browser checks** passed during the previous UI-fix phase;
  they were not rerun after these backend-only changes. Protected live UI was
  not visually checked in a signed-in browser. Expected simulated error logs
  in the test suites do not mean the suites failed.
- Citadel Securities' public WordPress REST discovery `/wp-json/` also returns
  403 (20 September); no new usable API established. Evidence:
  `/private/tmp/jw-citsec-rest.log`. Do not reclassify a technical refusal as
  proof that every future recovery route is impossible.
- Twelve inactive entries now: LinkedIn (policy), Gupshup, Krutrim, Flipkart,
  Zepto, Zomato, Blinkit, Instabase, **Siemens**, Citadel Securities, McKinsey,
  Ola. Older text describing Uber as blocked or only a candidate is superseded.
- **Next:** (1) inspect/wait for the existing Siemens process, never duplicate;
  (2) validate its result and enable/publish only if complete; (3) observe
  Microsoft's next normal priority resume after 12:11:22 and eventual full
  sweep, retaining conservative backoff; (4) verify normal Uber polling after
  its due time and observe existing Oracle tenants with the hardened parser;
  (5) continue other inactive-company work only from concrete permitted leads.
- Newly changed this continuation: `config.yaml`, `watcher/adapters/oracle.py`,
  `tests/test_oracle_completeness.py` (new), `watcher/runner.py`,
  `tests/test_runner.py`, and `watcher/adapters/README.md`, plus this handoff.
  Earlier UI/recovery edits remain uncommitted and must be preserved. The
  audit/recovery documents below may retain historical counts; this section is
  the authoritative current state.

## Earlier continuation — 20 September, 10:43 IST (superseded)

This section supersedes all older latest-state, test-count, no-deployment,
no-production-change, and "no UI fix needed" statements below. The user's
priority is Microsoft throttling, then the live site's company count, then this
handoff. Preserve the dirty worktree; nothing was committed. Do not spawn new
agents unless the current user/instructions authorize them; older authorization
recorded below is historical.

### Live company count: fixed and published

- Previously the home index contained only companies with matching open jobs:
  110 rows despite 128 active sources. The exporter now includes **all 128 active
  companies**, with empty shards for the 18 that have no matching open roles.
  Their home label is `No matching open roles`, without a bogus timestamp or
  new-job dot. The 13 inactive companies remain excluded; 141 tracked does not
  mean 141 monitored.
- `collect()` now also excludes disabled companies' stored jobs. This prevents
  an incomplete disabled-source baseline (such as Siemens) leaking into the
  published inventory. Disabled/removed company shards are cleaned up.
- Changed `watcher/export.py`, `watcher/site/app.js`, `tests/test_export.py`.
  Updated `devtools/site_check.py` to handle empty company screens and verify
  all watched companies, empty labels, no dots, and successful empty navigation.
- The regular scheduler deployed the assets on **19 September at 22:07 IST**.
  Production deployment `dd3aea70-2b6b-4a33-922d-ab580922eb5b` succeeded at
  `2026-09-19T16:37:08Z`; URL `https://dd3aea70.job-watch-live.pages.dev`.
  No duplicate deployment was necessary in this continuation.
- Read-only Cloudflare API verification on 20 September: the published KV
  index has **128 companies, watched=128, 18 empty, 2,152 matching jobs**, and
  zero inactive companies. Snapshot generated_at=1789880445. Counts can change.
  The recorded successful asset digest matches the current exported assets.
- Browser runtime had no connected browser. Used the repository's local
  Playwright phone-view harness instead: **all 26 checks passed**. Direct live
  asset requests were denied by the protected site; no signed-in live visual
  check was possible. Do not describe the local browser check as a live login.

### Microsoft: mitigation verified on chunks; full recovery still pending

- Existing mitigations are deployed through the scheduled working tree:
  incremental=false, requested timestamp sort without trusting early stop,
  max_pages_per_run=3, six-second jittered page pacing, one-hour resume minimum,
  24-hour cursor age, one-hour initial backoff, growing no-progress backoff,
  and Retry-After handling. No credentials, proxy, or challenge bypass used.
- After the cooldown, a **due-respecting**, silent real-runner check at
  **19 September 21:51:54 IST** fetched offsets 0/10/20 successfully: 30 jobs,
  total=238, valid cursor, no 429/backoff. It used `RunLock`, `sink=None`, and
  disabled export. It did not force seed or ignore scheduling/backoff.
- The normal scheduler restarted that sweep at **20 September 00:25:51**.
  The old log records only the generic restart, which could mean count drift
  or overlapping page IDs; do not assert which without evidence. Added distinct
  count-change/overlap log messages in `watcher/adapters/eightfold.py` so the
  next restart is diagnosable. Completeness protections are unchanged.
- Normal scheduler at **10:11:10–10:11:27 IST** successfully fetched another
  three pages, with no 429. Latest state: cursor start=30, ids=30, loc_ids=30,
  total=226; sweep_started=1789879270; last_polled=1789879270; no error/backoff.
  Earliest resume is **11:11:10 IST**, subject to scheduler rotation. No early
  request was made in this continuation.
- Latest completed inventory remains **239**, with last_complete epoch
  `1789757746` (19 September 00:25:46 IST, the sweep's start; completion was
  logged at 01:54). **Do not claim Microsoft is fully fixed.** Two successful
  chunks prove successful bounded access, not a coherent completed inventory
  or permanently eliminated throttling.
- Nine Eightfold tenants still share max_parallel=1 and max_per_run=1. Rotation
  and laptop sleep can greatly exceed the one-hour minimum; a 24-page sweep
  needs eight chunks and can drift before completion. Shared quota is an
  unconfirmed hypothesis. Do not relax count/overlap checks to manufacture
  completion or increase traffic merely to obtain a passing report.

### Recovery work completed before reprioritization

- **Siemens:** requested and verified stable `postedDate:ASC` ordering in the
  adapter; cursors persist that order. Legacy/other-order cursors restart
  without HTTP; missing/incorrect server sort echo rejects the response.
  Count and overlap safeguards remain. Tests cover those cases.
- One bounded silent production baseline was started. The user's Microsoft/UI
  priority interrupted it; only our own diagnostic was stopped, not launchd.
  Siemens remains **disabled, incomplete**: 102 known IDs, cursor offset=108,
  pending=6, total=953, order=postedDate:ASC, last_complete=None, no backoff.
  The KeyboardInterrupt in its temp log was intentional, not an access failure.
- **Uber:** found a concrete new public Oracle lead via a public integration's
  source. Anonymous API returned **20 India jobs with declared count=20** at
  `iaziqy.fa.ocs.oraclecloud.com`, site `CX_1`; robots returned 404. Titles include
  Uber for Business. This disproves treating prior Cloudflare failures as
  exhaustion of all leads. **Not yet production-ready or enabled.** Verify
  official employer attribution, full India coverage including secondary
  locations, details/URLs, and silent complete baseline before activation.
  Existing Oracle adapter does not recognize secondaryLocations[].Name; this
  response uses it. Fix/test that mapping before using the candidate source.
- Later permitted checks supersede early robots/TLS observations: Citadel's
  sitemap was fetched successfully using repository robots semantics (403 on
  robots was not treated as a universal prohibition). Modern Python/OpenSSL
  still received 403 from Zepto TalentRecruit, despite removing the old TLS
  failure. Gupshup exact APIs still 403; Instabase's linked Greenhouse source
  still 404; McKinsey search still timed out. No inactive company was enabled.

### Verification and next work

- **283 Python tests pass; 11 JavaScript tests pass; 26 local browser checks
  pass; git diff --check clean.** Expected simulated failure logs are normal.
- First observe Microsoft's next **normally due** chunk and subsequent sweep.
  Record exact new restart reason if any; assess conservative scheduling or a
  verified official alternative if multi-hour chunks cannot form a coherent
  inventory. Production proof of a complete sweep remains outstanding.
- If investigating inactive sources next, prioritize validating the **new Uber
  Oracle lead** and safely completing Siemens. No exhaustive impossibility
  claim is supported. Other company evidence remains in BLOCKED_SOURCE_REVIEW.md.
- Temporary evidence (may be removed by macOS):
  `/private/tmp/jw-microsoft-due-check.log`, `/private/tmp/jw-final-tests.log`,
  `/private/tmp/jw-final-js-tests.log`, `/private/tmp/jw-final-browser.log`,
  `/private/tmp/jw-siemens-sorted-baseline.{log,json}`,
  `/private/tmp/jw-uber-oracle-probe.log`, `/private/tmp/jw-recovery-review/`.
  Oracle sample body: `d6867a50712027c6.body`, manifest `uber-oracle.json` in
  the recovery-review directory. Public discovery source:
  `https://github.com/Averyy/fetchaller-mcp/blob/main/docs/site-apis.md`.
- Preserve existing source-recovery edits and notification behavior. Never
  print deploy env/plist secrets. Diagnostics remain silent; acquire RunLock
  for production writes and never kill the scheduler.

## Earlier recovery review — after the 21:30 handoff (superseded)

The user clarified: investigate every permitted recovery route before calling
companies blocked; do not add permanent enablement locks. Read
`BLOCKED_SOURCE_REVIEW.md` for the new direct checks across all twelve inactive
companies other than policy-excluded LinkedIn, including exact API evidence and
limits. No source was enabled and no runtime code or production state was changed
by this investigation.

No new production-ready recovery was established. **This is not proof that
recovery is impossible.** Siemens remains an accessible adapter awaiting a
coherent production baseline, not an access-blocked company. The newly checked
sitemap-advertised widget has only 21 detail links and points back to search;
it does not establish completeness. RSS still has 20 entries and the locale
sitemap 58 static routes with no concrete jobs.

Corrections to older observations: Zepto TalentRecruit's TLS failure recurred
on robots, so no new board response was obtained; Citadel Securities robots
returned 403, so dependent requests were stopped; Blinkit's inspected 403 is a
branded Access Denied page, not demonstrated to be a Cloudflare challenge.
Gupshup's exact v2 config/inventory/filter requests all still returned Cloudflare
403; its candidate apps themselves load. Uber sitemap still has 531 URLs but
listing and tested detail challenge. Instabase's exact embedded API and tested
linked detail still return 404. Ola/Flipkart robots restrictions remain.

Retain low-frequency recheck triggers and honest source validation. Never turn
these observations into an exhaustive assertion that no permitted source can
exist. Baseline checks in this turn passed 280 Python and 11 site tests, with
`git diff --check` clean.

Continue work in /Users/shashwattripathi/job-watch. Read this entire file before acting. The user wants Microsoft 429s reduced and correctly diagnosed, Ola tracked, and permitted company-source recoveries continued. Parallel agents were explicitly authorized for independent company investigations.

## 21:30 continuation update (superseded, 19 September)

This section supersedes any older "latest", "next actual turn", UI discrepancy,
test-count, or inactive-source wording later in this file. The older sections are
retained as history and detailed implementation context.

The user explicitly asked that the remaining inactive companies be investigated
as a portfolio using subagents, not by concentrating only on Siemens and Meta.
Three parallel read-only tracks rechecked all 12 inactive companies other than
policy-excluded LinkedIn. **None of the 12 can safely be enabled yet.** Do not
claim that a search-engine rendering, an indexed snippet, a reachable marketing
page, or a partial RSS feed is an automatable complete inventory.

### Work completed in this continuation

- Resolved the live Pages count discrepancy. Current config has 128 active
  sources. The current export has 2,169 open jobs that pass both title and
  location filters, grouped into exactly 110 companies, and separately records
  `watched: 128`. The missing 18 active companies simply have zero matching open
  rows; there is no UI cap, missing index entry, or stale snapshot bug. The
  successful KV publish hash matches the generated snapshot. No UI fix or
  deployment is needed. An optional future copy improvement could say
  "110 companies with matches · 128 watched".
- Confirmed the Microsoft legacy cursor transition in production. At 17:01:19
  IST the normal scheduler logged `backoff cleared` and
  `sweep changed while paging; restarting from the beginning next run`, with
  zero fetched rows. The adapter rejected the legacy cursor before HTTP. The DB
  now has `cursor=NULL`, `sweep_started=NULL`, no error/backoff, last_count=239,
  last_polled=17:01:19, and last_complete epoch 00:25:46. No later Microsoft
  HTTP request or 429 had occurred by 21:21. Subsequent runs respected the due
  delay or deferred Microsoft; at 21:14 Lam Research took the one-Eightfold-
  tenant slot. Fresh three-page progress and production proof of the new
  1h→2h→4h no-progress throttle escalation remain outstanding. Do not force a
  Microsoft request.
- Review found and fixed two correctness gaps:
  1. `CountedHTML` (Trakstar/SuccessFactors) now returns the explicit
     `restart=True` contract with zero rows on count drift or cross-page overlap,
     so abandoned-snapshot rows cannot remain falsely open.
  2. Meta now requires and validates `featured_jobs`, includes only featured
     rows whose own explicit locations establish India membership, and
     deduplicates them against `all_jobs`. Global/locationless featured promos
     are not inferred as India.
  Regression tests were added in `tests/test_followup_boards.py` and
  `tests/test_meta.py`. The stale top-of-config verification-date claim was
  corrected.
- Updated `config.yaml`, `AUTOMATION_AUDIT.md`, and
  `watcher/adapters/README.md` with the broad blocked-source recheck evidence.
- Verification after those code changes: **280 Python tests passed**, **11 site
  tests passed**, and `git diff --check` was clean. The suites intentionally
  print simulated HTTP/backoff/publish/notification failures before their final
  OK. Re-run all three after any further edit.
- No Siemens production baseline was started in this continuation. No source
  was enabled, no notifications were sent, and no deployment was triggered.

### Comprehensive recheck of the 12 inactive companies

1. **Uber — technical Cloudflare block.** Allowed
   `https://jobs.uber.com/en/jobs/sitemap.xml` currently has 531 unique job IDs
   plus lastmod values, but no title/location. Honest detail and filtered-list
   requests return 403 with `cf-mitigated: challenge`. Search-index copies show
   current India roles but are not a complete pollable source. Next trigger:
   build sitemap+detail only if direct details or an official read API become
   honestly accessible.
2. **Gupshup — technical Cloudflare block.** Official careers CTAs still use
   WhatsApp. Both Darwinbox candidate shells now load, including v2, and their
   bundles reveal `getCompanyConfig`, `job/alljobs`, and `job/filters`, but every
   exact read-only config/inventory request returns Cloudflare 403. Robots does
   not prohibit the paths. Next trigger: low-frequency recheck of those exact v2
   routes; if readable, require company identity, total count, pagination,
   stable IDs, and detail agreement before an adapter.
3. **Krutrim — LinkedIn-only official destination.** New official
   `https://ai-labs.olakrutrim.com/` has Join Us/Open Positions, but its own
   Next.js bundle proves the button opens Krutrim's LinkedIn Jobs page. LinkedIn
   is excluded by project policy. The old `www.olakrutrim.com/careers` remains
   404. Do not infer Krutrim from the shared Ola TurboHire organization.
4. **Flipkart — real board robots-disallowed.** The official nav still points to
   the known TurboHire board, whose ordinary-agent rule is `Disallow: /`.
   Permitted first-party `/jobslist` is only a static mock saying 69 openings
   while rendering 10 truncated cards with no IDs/links; Show More merely calls
   `alert('Loading more jobs...')`. Do not build an adapter for it.
5. **Zepto — WAF/403.** Official careers returns an AWS WAF 202 empty challenge
   and links `zepto.talentrecruit.com/career-page`. The TalentRecruit board and
   its robots URL now consistently return a plain 118-byte AWS ELB 403; the old
   TLS failure did not recur. Do not bypass either response. Recheck only the
   exact official board at low frequency.
6. **Zomato/Eternal — referral-only, no board.** The live Eternal careers page
   and component bundle make no job API request and explicitly accept
   applications through employee referrals. SmartRecruiters `Zomato1` is a real
   but abandoned three-row inventory from 2015–2016. Lever `eternal` belongs to
   an unrelated US athlete/performance company. Do not automate either.
7. **Blinkit — allowed route technically inaccessible.** Official
   `blinkit.com/careers/jobs` is not robots-disallowed but returns Cloudflare
   403. A current indexed rendering says 0 of 0, but that cannot be used as a
   baseline or future completeness signal. `jobs.blinkit.com` metadata calls it
   a Vendor Dashboard, not a verified careers board. Recheck the official
   careers route only.
8. **Instabase — dead official Greenhouse integration.** Official JS still
   hard-codes Greenhouse board `instabase`, but board/API return 404. Webflow
   exposes IDs 8042464002, 8073772002, 8084536002; every detail redirects to an
   error/404, cards contain placeholder text, and the 169-URL sitemap has zero
   job URLs. Recent indexed snippets are stale. Recheck the exact embedded
   Greenhouse API only; revive the normal Greenhouse adapter only after board,
   API, and details agree.
9. **Siemens — adapter valid, baseline still impossible on moving snapshot.** A
   later allowed India search declared 953 (diagnostic had coherently completed
   at 954). Robots-advertised locale sitemap has 58 static portal routes and
   zero job URLs. Permitted India RSS returns exactly 20 items with IDs/titles
   but no location/count and is not proven newest-first; it cannot prove
   closures or replace the counted HTML sweep. Keep `enabled: false`. Do not
   launch another 159-page validation/baseline until there is a defensible
   coherence design or a genuinely quieter bounded attempt; RSS may only be a
   change sentinel, never completeness evidence.
10. **Citadel Securities — technical Cloudflare block.** Career sitemap is
    readable and currently has 88 detail slugs, but no requisition IDs, title
    field, or location; its generator lastmods are not posting dates. Honest
    details return Cloudflare challenges, and some slugs are evergreen interest
    pages. A future sitemap+detail adapter is possible only if direct details
    become readable; never infer location from slugs/snippets.
11. **McKinsey — direct network timeout.** Search-engine copies prove current
    official jobs exist (example req 104473 in Bengaluru/Chennai/Gurugram), but
    watcher-style requests to search, detail, and sitemap hang with zero bytes
    at 30s+. Robots permits the career route. `jobs.mckinsey.com` is a reachable
    Avature candidate/application portal whose Open Positions link returns to
    the inaccessible main search; it contains no listing. Indexed copies are
    not automation evidence.
12. **Ola Mobility/Electric — robots-disallowed shared TurboHire board.** Both
    official careers pages are readable, expose no inventory, and link the same
    org `e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212` using different TurboHire routes.
    `olacareers.turbohire.co/robots.txt` remains `User-agent: * / Disallow: /`;
    later Googlebot exceptions do not apply. No board path was fetched. This
    proves Mobility/Electric sharing only, not Krutrim coverage.

### Exact next-session priorities

1. Read this update, then `AUTOMATION_AUDIT.md`, `git status`, and relevant
   diffs. Preserve every tracked/untracked user change. Nothing is committed.
2. Re-run `PYTHONWARNINGS=ignore .venv-dev/bin/python -m unittest discover -q`,
   `node --test tests/site_state.test.mjs`, and `git diff --check` to confirm the
   280/11 baseline after handoff edits.
3. Observe normal Microsoft scheduling only. Confirm its first fresh replacement
   chunk starts at offset 0, fetches no more than three pages, stores new
   `loc_ids`, and waits at least one hour before resume. If a no-progress 429
   occurs, verify the next waits grow 1h, then 2h, then 4h; real progress must
   reset escalation. Do not claim the quota is known or all 429s are solved.
4. Observe normal Meta, BookMyShow, and Chargebee polls after the review fixes.
   Confirm Meta's live response still contains a list-shaped `featured_jobs` and
   that current inventory remains coherent. Do not silently rebaseline or force
   a source unless production state actually requires it.
5. For blocked-company work, use the exact future triggers above. Do not repeat
   broad ATS guessing or hammer proven failures. The best concrete recheck
   anchors are Gupshup Darwinbox v2 APIs, Uber sitemap+direct details, Zepto's
   officially linked TalentRecruit board, Blinkit's official careers route,
   Instabase's embedded Greenhouse API, and McKinsey direct search/sitemap.
6. Keep Siemens disabled. A cheap source must provide stable identity, title,
   location, and full membership/closure evidence; the 20-item RSS and static
   sitemap do not.
7. Update this handoff and audit whenever production state changes. Reports must
   continue to separate implementation/tests, live diagnostic validation,
   silent production baseline, normal monitoring, and unresolved limitations.

## Operating constraints

- Preserve every tracked and untracked change. Nothing is committed. Do not reset, discard, overwrite, or casually reformat unrelated work.
- Check for AGENTS.md in the repo/ancestors. None was found previously; unrelated copies in Go caches do not apply.
- Use the repository's honest HTTP client and obey robots. No challenge solving, browser impersonation, Googlebot spoofing, proxies, or alternate-host tricks. Ordinary public frontend Referer/Origin headers are acceptable.
- Never print deploy/jobswatch.env, LaunchAgent credentials, tokens, or notification secrets. Do not message, apply, subscribe, or create job alerts.
- Do not deploy Cloudflare unnecessarily. Diagnostics and baselines must disable exports and notifications.
- Respect production RunLock; never kill the scheduler. Working source, unit tests, complete diagnostic scan, config registration, silent production baseline, and active monitoring are separate milestones.
- Use .venv-dev/bin/python. Config uses watcher.yamlish; load it with watcher.config.load.
- Use apply_patch for repo edits. The worktree is intentionally dirty.

## Current verified state

watcher.config.active_companies currently reports 141 tracked, 128 active, 13 inactive. Inactive: linkedin, uber, gupshup, krutrim, flipkart, zepto, zomato, blinkit, instabase, siemens, citadelsecurities, mckinsey, ola.

The user currently sees only **110 companies** at
https://job-watch-live.pages.dev/#/. Treat this as an unresolved production/UI
discrepancy. Active polling count (128) is not necessarily the same as displayed
company count: the export may intentionally include only companies with open
matching rows, or the KV/site snapshot may be stale. Do not assume which one.
The next session must compare current DB active companies, companies with open
matches, the exported snapshot/KV metadata, and the UI's company-index logic.
Do not expose Access credentials or trigger an unnecessary deployment while
diagnosing it.

AUTOMATION_AUDIT.md and watcher/adapters/README.md are now current. They replace older claims that Meta only returned 400, Bain's API could not work, or enabled recovered sources were unreachable.

Latest complete validation after integration:

- PYTHONWARNINGS=ignore .venv-dev/bin/python -m unittest discover -q: 277 tests passed.
- node --test tests/site_state.test.mjs: 11 tests passed.
- git diff --check: clean.

The Python suite intentionally prints simulated backoff, publish failure, network, and notification warnings; its final result was OK. Re-run all three after new edits.

## Enabled and silently production-baselined recoveries

All twelve are active and completed cursor-free, error-free silent baselines with no backlog notification:

| Company | Source/scope | Baseline |
| --- | --- | ---: |
| Akamai | Oracle Recruiting Cloud pod, site CX_1, India | 19 |
| Caterpillar | Workday cat/wd5/CaterpillarCareers, India facet c4f78be1a8f14da0ab49ce1162348a5e | 97 |
| Nutanix | Jobvite token nutanix, India region | 38 |
| Navi | First-party navi.com/api/careers/jobs | 41 |
| Qdrant | Official About Us → qdrant.to/jobs → Ashby qdrant.tech | 5 |
| Vespa | Allowed BambooHR /careers/list | 9 |
| GreyOrange | Zwayam company 16090 | 88 |
| HSBC | Eightfold apply API, portal.careers.hsbc.com, hsbc.com, India | 167 |
| BookMyShow | Trakstar HTML; detail identity and public RSS IDs agree | 13 |
| Chargebee | SuccessFactors HTML; organization verified | 1 |
| Bain | Official counted careers API | 326; 16 matches |
| Meta | Anonymous public location/search operations, India | 24; 3 matches |

Qualifications: Caterpillar timestamps are unordered, so it uses complete India sweeps. Navi's Ashby navi token is a different company. Qdrant Remote India counts, while Europe-only remote does not imply India. Vespa /jobs/embed2.php remains disallowed. BookMyShow identity was verified from details/RSS because the main marketing site returned 403. Chargebee's one current job is Remote US, so strict filtering yields no India match.

## Bain — completed and enabled

New adapter/tests: watcher/adapters/bain.py and tests/test_bain.py. It is registered and configured as ats: bain.

Public request:
GET https://www.bain.com/en/api/jobsearch/keyword/get?start=0&results=400&filters=&searchValue=
Referer: https://www.bain.com/careers/find-a-role/

No cookie, login, special User-Agent, or browser impersonation is needed. start is a zero-based page number. Ten-row ordering is unstable: with totalResults still 326, job 2138182 crossed the page-29/30 boundary and 2138175 shifted backward. Strict pagination safely reset but could livelock. A 400-row request returns the whole current inventory while pagination remains for future growth.

Evidence /private/tmp/jw-bain-validation-complete.json has 326 rows/326 unique, complete=true, cursor null. The older /private/tmp/jw-bain-validation.json is an incomplete 300-row diagnostic and is not a baseline. Three roles explicitly omit location: 10445, 102466, 100635. They stay in inventory but cannot pass the strict India/city filter.

Production baseline completed 16:12 IST: 326 known/open, 16 configured matches, no cursor/error, sink=None, seed=True, exports disabled.

## Meta — location gap resolved, enabled and baselined

New adapter/tests: watcher/adapters/meta.py and tests/test_meta.py. MetaAdapter is registered as ats: meta. Config uses country India, hourly polling, one-second pacing, and strict India/city locations without generic remote.

Each poll makes three allowed anonymous requests using the normal watcher User-Agent:

1. GET https://www.metacareers.com/jobsearch/ and parse fresh LSD/SiteData.
2. POST https://www.metacareers.com/api/graphql/ with CareersJobSearchLocationFilterV3Query, current doc ID 24867916029505828, to discover India offices.
3. POST the same API with CareersJobSearchResultsDataQuery, current doc ID 27506805582236862, filtered by those office display names.

The request uses ordinary Origin/Referer, X-FB-LSD, fresh page context, and form fields. Cookies are unnecessary. Never hardcode transient page tokens. Persisted query IDs can change; query errors/shape changes must fail without closing jobs.

Completeness evidence:

- Global all_jobs: 989 unique.
- One distinct featured job: global + featured = 990 IDs.
- Those 990 IDs exactly equal the allowed job sitemap's 990 IDs.
- Four global jobs have locations=[]: 1115072141178813, 1064155186370895, 3492253594282417, 2358713911326793.
- India filters were Bangalore, Gurgaon, Hyderabad, Mumbai, New Delhi.
- India search returned 24 unique jobs, zero empty locations, exactly the global rows with explicit India locations. None of the four locationless IDs appeared.

Evidence files: /private/tmp/jw-meta-search-query.json, /private/tmp/jw-meta-search-no-cookie.json, /private/tmp/jw-meta-india-query.json.

Review caught and fixed a bad synthesized URL: /jobs/{id}/ returns 404. The adapter now uses the verified /profile/job_details/{id}/ route.

Silent baseline completed 16:21 IST: 24 known/open, three matches, no cursor/error/backoff, no notification/export.

The older watcher/adapters/schema_sitemap.py remains only a disabled historical prototype and is still tested.

## Siemens — validated adapter, deliberately disabled

Files: watcher/adapters/siemens.py and tests/test_siemens_followup.py. The adapter is registered, and config retains its settings, but enabled: false must remain until a production baseline completes.

Permitted India-faceted search:
https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?42386=%5B812053%5D&42386_format=17546&listFilterMode=1&folderRecordsPerPage=6&folderOffset=0

The server always returns six rows/page. Totals changed 955 → 957 → 954. The adapter validates displayed ranges, next links, duplicates, count drift, exact detail Job ID, off-host links, resume state, and pending multi-location details. It supports Location(s), Any Siemens location in, and structured plain Location:.

The coherent /private/tmp/jw-siemens-validation.json has 954 rows/954 unique, no empty title/location, no identity/URL mismatch, and 120 multi-location rows. Strict India/city accepts 951; default title + location gives 82 matches. Facet anomalies retained but excluded:

- 516009: Toronto / Troy
- 517911: Detroit / Mexico City
- 493275: Any Siemens location in the world

Production baseline did not complete. Five bounded attempts using the real runner detected live count/page drift, emitted restart with zero rows, and left production cursor-free with zero known Siemens IDs. The loop was stopped to avoid excess traffic. Do not claim a Siemens baseline or enable it because the diagnostic completed.

Next: wait for a quiet window and make at most one bounded silent attempt, stopping on restart. Or improve coherence semantics while proving no missed/false-closed jobs under mutable six-row ordering. Do not repeatedly hammer the 159-page board.

## Microsoft 429 work

Log: /Users/shashwattripathi/Library/Logs/jobswatch/jobswatch.err.log.

Inspected warnings: Microsoft 13 timestamped 429s, Qualcomm 3, no other inspected Eightfold tenant 429s. A shared tenant/IP quota is only a hypothesis. Responses disclosed no quota headers; the successful diagnostic exposed no Retry-After or rate-limit header.

Timeline:
- Sep 18: throttles at offsets 0, 120, 200, 210; later completed 235. Further throttles at 40, 80, 100.
- Sep 18 23:21: old three-hour cursor expired; timestamp ordering failed; incremental fallback duplicated work before throttling.
- Sep 19: throttles at 90, 10, 30, 90. At 14:43 the first offset-90 request throttled after roughly 136 idle minutes. Six-second spacing alone does not prevent 429.
- One post-cooldown diagnostic offset-90 request returned 200, count 239, ten rows, no quota/Retry-After header, and did not mutate production.

Implemented:
- Microsoft incremental=false; sort_by=timestamp; page_delay=6; interval=3600; resume_interval=3600; max_pages_per_run=3; cursor_max_age=86400; backoff_min=3600.
- Timestamp sorting is requested for stability but never trusted for early stop, avoiding failed probe + duplicate full fetch.
- Partial cursors obey a one-hour minimum.
- No-progress throttle state is preserved and delays grow 1h, 2h, 4h. Real progress resets escalation. Retry-After is honored/logged.
- Eightfold validates shape, count drift, per-location IDs, overlap, incomplete pages, and legacy cursor shape.
- FetchResult.restart was added. The runner clears cursor and the old sweep_started epoch on a restart. This fixes abandoned-only rows potentially remaining falsely open. Bain, Siemens, and Zwayam use the same contract.

Nine Eightfold companies share max_parallel=1 and max_per_run=1. Fair rotation at 15-minute launches can take ~2.25h. Microsoft needs ~24 pages, so eight three-page chunks can take ~18h even without throttling/drift. One-hour settings do not promise hourly completed inventories.

Latest read-only snapshot at 16:24 IST:
- last poll 14:43
- last complete 239
- legacy cursor at offset 90 with 90 IDs and no loc_ids
- scheduler run ending 16:12 deferred Microsoft and warned no complete sweep for 15.8h

The next actual Microsoft turn should detect the legacy cursor and return restart=True without HTTP, clearing cursor and epoch. This was not yet observed. Do not force a Microsoft request. Inspect normal scheduler logs/status. Later verify fresh three-page chunks obey the one-hour gap and no-progress throttles escalate. Do not claim all 429s are solved or the quota is known.

## Ola and remaining inactive sources

Ola is tracked as blocked/robots. Honest fetches of olacabs.com/careers and olaelectric.com/careers both link TurboHire org e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212. Mobility uses /careerpage/<org>; Electric uses /dashboardv2?orgId=<org>&type=0. The board robots file disallows /. No board paths were fetched. This does not establish Krutrim membership.

Other inactive evidence:

- LinkedIn: excluded by policy.
- Uber: sitemap omits title/location; details 403; old API/custom-API robots-disallowed.
- Gupshup: official page offers WhatsApp; real Darwinbox app exists but public job/company endpoints 403. Do not say portal switched off.
- Krutrim: careers 404, sitemap product-only, no verified board.
- Flipkart: official TurboHire robots-disallowed.
- Zepto: AWS WAF 202; linked TalentRecruit gives TLS failure/403.
- Zomato: Eternal exposes referral/contact, no enumerated openings.
- Blinkit: careers/jobs 403; not proof of zero jobs.
- Instabase: rechecked. Official JS hard-codes dead Greenhouse instabase. Two Webflow CMS pages expose IDs 8042464002, 8073772002, 8084536002; all details 404, cards contain placeholders, 169-URL sitemap has no jobs.
- Citadel Securities: career sitemap readable; listings/details 403. Snippets cannot prove current inventory/location; some entries are evergreen interest.
- McKinsey: search/sitemap time out; candidate portal has no verified listing.

Continue only from concrete permitted leads. Do not rerun proven blocked endpoints or claim exhaustive proof no source exists.

## Production mechanics

DB: /Users/shashwattripathi/.local/share/jobswatch/jobs.db; lock is .lock beside it. LaunchAgent: /Users/shashwattripathi/Library/LaunchAgents/com.shashwattripathi.jobswatch.plist. It runs /usr/bin/python3 -m watcher run every 15 minutes while awake. Never dump plist/env secrets.

Silent baseline procedure:
1. Keep file config disabled until source validation/tests complete.
2. Enable candidate only in memory.
3. Set cfg['export']={'enabled': False}.
4. Acquire RunLock(DB+'.lock'); if scheduler owns it, wait, never kill.
5. runner.run(cfg, store, seed=True, sink=None, only={candidate}).
6. Require result.ok, cursor null, last_complete set, no error/backoff, exact known count, no export/notification.
7. Only then enable file config.

Bain and Meta followed this flow. The first Bain attempt hit the scheduler lock and wrote nothing; retry succeeded. Siemens failed completion and was re-disabled.

## Worktree

Modified tracked files:
README.md; config.yaml; tests/test_eightfold_incremental.py; tests/test_new_adapters.py; tests/test_runner.py; tests/test_workday.py; watcher/adapters/README.md; watcher/adapters/__init__.py; watcher/adapters/ashby.py; watcher/adapters/base.py; watcher/adapters/eightfold.py; watcher/adapters/workday.py; watcher/runner.py; watcher/store.py.

Untracked/new:
AUTOMATION_AUDIT.md; SESSION_HANDOFF.md; tests/test_bain.py; tests/test_followup_boards.py; tests/test_meta.py; tests/test_recovered_boards.py; tests/test_siemens_followup.py; watcher/adapters/bain.py; bamboohr.py; jobvite.py; meta.py; navi.py; public_html.py; schema_sitemap.py; siemens.py; zwayam.py.

Nothing is committed. Preserve strict Ashby fields, Workday facets on every page, counted HTML safeguards, partial throttle handling, and safe cursor/restart logic.

## Recommended next-session order

1. Read this file, AUTOMATION_AUDIT.md, git status, and relevant diffs. Confirm no process remains from an interrupted monitor command; do not start a duplicate Siemens validator.
2. Observe normal Microsoft scheduling. Confirm legacy offset-90 cursor clears via restart without HTTP. Later inspect fresh progress/resume/backoff behavior.
3. Re-run 277 Python tests, 11 JS tests, and git diff --check after any edit. Review all diffs for stale comments and unsafe URLs.
4. Reassess Siemens only after a quiet interval. Make at most one bounded silent baseline attempt; stop on restart. Prove completeness before enabling.
5. Observe normal Meta/Bain polls after their one-hour due times. Confirm Meta doc IDs still work and both inventories stay complete.
6. Diagnose why the live Pages UI shows 110 companies while config has 128
   active. First determine whether 110 exactly equals the companies represented
   by open matching rows in the latest exported snapshot. Then check whether the
   snapshot timestamp and company index are current. Fix only if the UI/export
   is actually omitting companies it is designed to show.
7. Continue inactive-company research only from concrete permitted leads.
8. Update this handoff/audit when state changes. Final reports must separate implementation/tests, diagnostic validation, production baseline, active monitoring, and unresolved limits.
