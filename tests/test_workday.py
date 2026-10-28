"""Workday adapter against pages recorded from rbs.wd3 (NatWest) on 2026-09-16.

The recording has total=89 on offset 0, total=0 on later pages, 9 rows at
offset 80, and offset 100 (past the end) returning page one again — the
wrap-around.
"""

import unittest
from unittest import mock

from tests.fakes import FakeHttp, fixture
from watcher.adapters import AdapterError, FetchContext
from watcher.adapters import workday as wd
from watcher.adapters.workday import WorkdayAdapter
from watcher.http import HttpStatusError

CFG = {"ats": "workday", "tenant": "rbs", "shard": "wd3", "sites": ["RBS"]}
JOBS = "https://rbs.wd3.myworkdayjobs.com/wday/cxs/rbs/RBS/jobs"


def page(offset):
    return fixture(f"workday_rbs/offset_{offset}.json")


def board(pages):
    """Route POST /jobs by offset to the given {offset: response}."""

    def handler(method, url, payload, cache):
        off = payload["offset"]
        if off not in pages:
            raise AssertionError(f"unexpected request for offset {off}")
        return pages[off]

    return FakeHttp({JOBS: handler})


def offsets(http):
    return [c[2]["offset"] for c in http.calls]


class RecordedBoard(unittest.TestCase):
    def test_discovers_all_current_india_offices_without_indiana(self):
        facets = [{"facetParameter": "locationMainGroup", "values": [{
            "facetParameter": "locations", "values": [
                {"descriptor": "Office - India - Bangalore", "id": "blr", "count": 1},
                {"descriptor": "Navi Mumbai, India (Finicity)", "id": "navi", "count": 1},
                {"descriptor": "Remote - Indiana", "id": "indiana"},
                {"descriptor": "Indianapolis, United States", "id": "indy"},
            ],
        }]}]

        def handler(method, url, payload, cache):
            if not payload["appliedFacets"]:
                return {"total": 999, "facets": facets, "jobPostings": []}
            self.assertEqual(payload["appliedFacets"], {"locations": ["blr", "navi"]})
            return {"total": 1, "jobPostings": page(80)["jobPostings"][:1]}

        http = FakeHttp({JOBS: handler})
        cfg = dict(CFG, country_locations="India", location_hint="India", require_exact_count=True)
        result = WorkdayAdapter("natwest", cfg, http=http, page_delay=0).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 1)
        self.assertEqual(http.requests, 2)
        # A newly published office joins the next sweep without a config edit.
        facets[0]["values"][0]["values"].append({"descriptor": "Pune, India", "id": "pune", "count": 1})
        a = WorkdayAdapter("natwest", cfg, http=http, page_delay=0)
        self.assertEqual(a.country_location_facets(), {"locations": ["blr", "navi", "pune"]})

    def test_ignored_country_location_filter_fails_closed(self):
        def handler(method, url, payload, cache):
            return {"total": 99, "jobPostings": page(80)["jobPostings"][:1], "facets": [{
                "facetParameter": "locations", "values": [
                    {"descriptor": "Pune, India", "id": "pune", "count": 1},
                ],
            }]}

        adapter = WorkdayAdapter("natwest", dict(CFG, country_locations="India"),
                                 http=FakeHttp({JOBS: handler}), page_delay=0)
        with self.assertRaisesRegex(AdapterError, "filter was not honored"):
            adapter.fetch()

    def test_missing_india_offices_fail_closed(self):
        http = FakeHttp({JOBS: {"total": 99, "facets": [], "jobPostings": []}})
        adapter = WorkdayAdapter("natwest", dict(CFG, country_locations="India"),
                                 http=http, page_delay=0)
        with self.assertRaisesRegex(AdapterError, "refusing closures"):
            adapter.fetch()

    def test_configured_country_facet_is_sent_on_every_page(self):
        http = board({o: page(o) for o in (0, 20, 40, 60, 80)})
        facets = {"locationCountry": ["india-id"]}
        WorkdayAdapter("natwest", dict(CFG, applied_facets=facets), http=http, page_delay=0).fetch()
        self.assertTrue(all(c[2]["appliedFacets"] == facets for c in http.calls))

    def test_stops_on_total_from_first_page(self):
        http = board({o: page(o) for o in (0, 20, 40, 60, 80, 100)})
        res = WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()
        self.assertTrue(res.complete)
        self.assertEqual(len(res.postings), 89)
        self.assertEqual(len({p.job_id for p in res.postings}), 89)
        self.assertEqual(offsets(http), [0, 20, 40, 60, 80])
        self.assertTrue(all(c[2]["limit"] == 20 for c in http.calls))

    def test_req_id_is_not_blindly_first_bullet(self):
        http = board({o: page(o) for o in (0, 20, 40, 60, 80)})
        postings = WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch().postings
        first = postings[0]
        self.assertEqual(first.raw["bulletFields"][0], "Hybrid")
        self.assertEqual(first.req_id, "R-00284752")
        self.assertEqual(first.job_id, "R-00284752-1")
        self.assertTrue(first.url.startswith("https://rbs.wd3.myworkdayjobs.com/en-US/RBS/job/"))


class WrapAround(unittest.TestCase):
    """Offset past the end returns rows again. The loop must still terminate."""

    def eighty_row_board(self, total):
        """80 real rows, then the wrap: every later offset serves page one again.

        More than one wrapped page is stubbed on purpose. A single all-duplicate
        page is NOT proof of the end -- Walmart produces one mid-sweep because
        its ordering is unstable -- so the adapter waits for a run of them.
        """
        pages = {o: page(o) for o in (0, 20, 40, 60)}
        pages[0] = dict(pages[0], total=total)
        for o in (80, 100, 120, 140):
            pages[o] = page(100)       # recorded: past the end → page one again
        return pages

    def test_terminates_on_wrap_when_total_missing(self):
        http = board(self.eighty_row_board(total=0))
        res = WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 80)          # the wrapped pages add nothing
        # three consecutive duplicate pages, then it stops
        self.assertEqual(offsets(http), [0, 20, 40, 60, 80, 100, 120])

    def test_one_duplicate_page_mid_sweep_does_not_end_the_sweep(self):
        """The Walmart bug: an unstable ordering yields an all-duplicate page in
        the middle of a healthy board. Stopping there gave 360 of 2000, failed
        the coverage check, and the sweep never completed -- so closed reqs were
        never retired and the site kept linking to dead postings."""
        pages = {0: dict(page(0), total=0),     # total unknown: isolate the dup rule
                 20: page(0),                    # <- the duplicate, mid-sweep
                 40: page(20), 60: page(40), 80: page(60)}
        for o in (100, 120, 140):
            pages[o] = page(60)                  # the real wrap, at the end
        http = board(pages)
        res = WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()
        # It read straight past the duplicate at offset 20 and kept collecting,
        # stopping only on three consecutive duplicate pages at the end.
        self.assertEqual(offsets(http), [0, 20, 40, 60, 80, 100, 120, 140])
        self.assertEqual(len(res.postings), 80)

    def test_terminates_on_wrap_when_total_overstated_slightly(self):
        http = board(self.eighty_row_board(total=84))
        with self.assertLogs(wd.log, "WARNING"):
            res = WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 80)
        self.assertEqual(offsets(http), [0, 20, 40, 60, 80])

    def test_short_sweep_is_an_error_not_a_mass_closure(self):
        http = board(self.eighty_row_board(total=200))
        with self.assertRaises(AdapterError):
            WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()

    def test_page_cap(self):
        def endless(method, url, payload, cache):
            off = payload["offset"]
            rows = [{"title": "x", "externalPath": f"/job/X/T_R-{off + i}", "bulletFields": []} for i in range(20)]
            return {"total": 0, "jobPostings": rows}

        http = FakeHttp({JOBS: endless})
        with self.assertRaises(AdapterError):
            WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()
        self.assertEqual(http.requests, wd.MAX_PAGES)


class Resumable(unittest.TestCase):
    def test_changing_country_facet_restarts_global_cursor_without_fetching(self):
        http = board({})
        adapter = WorkdayAdapter("natwest", dict(CFG, applied_facets={"country": ["india"]}),
                                 http=http, page_delay=0)
        result = adapter.fetch(FetchContext(cursor={
            "site": 0, "offset": 20, "total": 89, "ids": ["old"], "site_ids": ["old"],
        }))
        self.assertTrue(result.restart)
        self.assertFalse(result.complete)
        self.assertEqual(result.postings, [])
        self.assertEqual(http.requests, 0)

    def test_country_scope_survives_pause_and_resume(self):
        http = board({o: page(o) for o in (0, 20, 40, 60, 80)})
        facets = {"country": ["india"]}
        adapter = WorkdayAdapter("natwest", dict(CFG, applied_facets=facets),
                                 http=http, page_delay=0)
        with mock.patch.object(wd.time, "monotonic", return_value=999):
            first = adapter.fetch(FetchContext(deadline=10))
        self.assertEqual(first.cursor["scope"], facets)
        second = adapter.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.complete)
        self.assertEqual(len(first.postings) + len(second.postings), 89)

    def test_pauses_at_deadline_and_resumes_with_overlap(self):
        http = board({o: page(o) for o in (0, 20, 40, 60, 80)})
        adapter = WorkdayAdapter("natwest", CFG, http=http, page_delay=0)
        ticks = iter([0.0, 1.0, 999.0])
        with mock.patch.object(wd.time, "monotonic", lambda: next(ticks, 999.0)):
            first = adapter.fetch(FetchContext(deadline=10.0))
        self.assertFalse(first.complete)
        self.assertEqual(offsets(http), [0, 20, 40])
        self.assertEqual(first.cursor["offset"], 60)
        self.assertEqual(first.cursor["total"], 89)

        http.calls.clear()
        second = adapter.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.complete)
        self.assertEqual(offsets(http), [40, 60, 80])      # one page of overlap
        ids = {p.job_id for p in first.postings} | {p.job_id for p in second.postings}
        self.assertEqual(len(ids), 89)
        self.assertFalse({p.job_id for p in first.postings} & {p.job_id for p in second.postings})


class Detail(unittest.TestCase):
    def test_location_hint_keeps_authoritative_country_on_multi_location_row(self):
        cfg = dict(CFG, location_hint="India")
        adapter = WorkdayAdapter("filtered", cfg, http=FakeHttp({}), page_delay=0)
        p = adapter._posting("RBS", {
            "title": "Platform Engineer", "externalPath": "/job/X/Platform-Engineer_R1",
            "locationsText": "2 Locations", "postedOn": "Posted Today", "bulletFields": ["R1"],
        })
        self.assertEqual(p.location, "2 Locations | India")
        self.assertTrue(adapter.needs_enrich(p))

    def test_enrich_resolves_multi_location(self):
        detail = fixture("workday_detail_salesforce.json")
        cfg = {"tenant": "salesforce", "shard": "wd12", "sites": ["External_Career_Site"]}
        path = "/job/Georgia---Atlanta/Senior-Manager--Software-Engineering_JR356609-2"
        url = f"https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site{path}"
        adapter = WorkdayAdapter("salesforce", cfg, http=FakeHttp({url: detail}), page_delay=0)
        p = adapter._posting("External_Career_Site", {
            "title": "Senior Manager, Software Engineering", "externalPath": path,
            "locationsText": "3 Locations", "postedOn": "Posted Today", "bulletFields": ["JR356609"],
        })
        self.assertEqual(p.job_id, "JR356609-2")
        self.assertEqual(p.req_id, "JR356609")
        self.assertTrue(adapter.needs_enrich(p))
        adapter.enrich(p)
        self.assertFalse(adapter.needs_enrich(p))
        self.assertIn("California - San Francisco", p.location)

    def test_bad_site_is_config_error(self):
        http = FakeHttp({JOBS: HttpStatusError(JOBS, 404)})
        with self.assertRaises(AdapterError):
            WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()

    def test_throttle_is_not_swallowed(self):
        http = FakeHttp({JOBS: HttpStatusError(JOBS, 429, retry_after=120)})
        with self.assertRaises(HttpStatusError):
            WorkdayAdapter("natwest", CFG, http=http, page_delay=0).fetch()


if __name__ == "__main__":
    unittest.main()


def synthetic(n, ages):
    """A newest-first board of n postings; ages[i] is posting i's age in days."""
    def row(i):
        age = ages[i]
        when = "Posted Today" if age == 0 else "Posted Yesterday" if age == 1 else f"Posted {age} Days Ago"
        return {"title": f"Engineer {i}", "externalPath": f"/job/Place/Engineer-{i}_R{i:05d}",
                "locationsText": "Bengaluru", "postedOn": when, "bulletFields": [f"R{i:05d}"]}

    def handler(method, url, payload, cache):
        off = payload["offset"]
        return {"total": n if off == 0 else 0, "jobPostings": [row(i) for i in range(off, min(off + 20, n))]}
    return FakeHttp({JOBS: handler})


class Incremental(unittest.TestCase):
    """Wells Fargo re-read ~1,800 postings every sweep to find a handful of new
    ones. On a tenant whose default order is newest first, a poll can stop once
    it is back on known ground -- as long as it never pretends to be a sweep."""

    CFG = dict(CFG, incremental=True)
    NOW = 1_800_000_000

    def ctx(self, known, last_complete_ago=3600):
        return FetchContext(known=set(known), last_complete=self.NOW - last_complete_ago, now=self.NOW)

    def ids(self, rng):
        return {f"R{i:05d}" for i in rng}

    def test_stops_after_two_pages_of_known_postings(self):
        http = synthetic(400, [i // 20 for i in range(400)])
        known = self.ids(range(5, 400))                        # 0-4 are new, at the top
        res = WorkdayAdapter("wf", self.CFG, http=http, page_delay=0).fetch(self.ctx(known))
        self.assertTrue(res.incremental)
        self.assertFalse(res.complete)                          # never a sweep: retires nothing
        self.assertEqual(offsets(http), [0, 20, 40])            # page with news, then two known pages
        self.assertTrue(self.ids(range(5)) <= {p.job_id for p in res.postings})

    def test_an_order_that_is_not_newest_first_falls_back_to_a_full_sweep(self):
        ages = [i // 20 for i in range(100)]
        ages[30] = 0                                            # a fresh posting deep in the list
        http = synthetic(100, ages)
        res = WorkdayAdapter("walmart", self.CFG, http=http, page_delay=0).fetch(self.ctx(self.ids(range(100))))
        self.assertFalse(res.incremental)
        self.assertTrue(res.complete)                           # swept, in the same run
        self.assertEqual(len(res.postings), 100)

    def test_a_full_sweep_still_runs_every_full_sweep_interval(self):
        http = synthetic(60, [0] * 60)
        res = WorkdayAdapter("wf", self.CFG, http=http, page_delay=0).fetch(
            self.ctx(self.ids(range(60)), last_complete_ago=wd.FULL_SWEEP + 1))
        self.assertFalse(res.incremental)
        self.assertTrue(res.complete)

    def test_so_much_new_that_sweeping_is_cheaper(self):
        n = 20 * (wd.INCREMENTAL_MAX_PAGES + 3)
        http = synthetic(n, [0] * n)
        res = WorkdayAdapter("wf", self.CFG, http=http, page_delay=0).fetch(self.ctx({"old"}))
        self.assertTrue(res.complete)                           # gave up on the shortcut

    def test_off_unless_configured_and_never_on_an_unseeded_board(self):
        http = synthetic(60, [0] * 60)
        res = WorkdayAdapter("x", CFG, http=http, page_delay=0).fetch(self.ctx(self.ids(range(60))))
        self.assertTrue(res.complete)                           # no `incremental: true`
        http = synthetic(60, [0] * 60)
        res = WorkdayAdapter("x", self.CFG, http=http, page_delay=0).fetch(FetchContext(now=self.NOW))
        self.assertTrue(res.complete)                           # nothing known yet: sweep

    def test_posted_age(self):
        self.assertEqual([wd.posted_age(s) for s in ("Posted Today", "Posted Yesterday", "Posted 3 Days Ago",
                                                     "Posted 30+ Days Ago", "Closing soon")],
                         [0, 1, 3, 30, None])
