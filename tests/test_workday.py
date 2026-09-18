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
