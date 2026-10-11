import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.bain import BainAdapter, DEFAULT_PAGE_SIZE, REFERER
from watcher.adapters.base import AdapterError, FetchContext
from watcher.http import HttpStatusError
from watcher.filters import Filter


def row(n, **changes):
    return dict({"JobId": str(n), "JobTitle": "Software Engineer",
                 "Link": f"/careers/find-a-role/position/?jobid={n}",
                 "Location": [" London ", " Bengaluru "]}, **changes)


def page(start, stop, total):
    return {"results": [row(n) for n in range(start, stop)], "totalResults": total}


@patch.object(BainAdapter, "check_robots")
class BainSafety(unittest.TestCase):
    def adapter(self, responses, **cfg):
        cfg.setdefault("page_size", 10)
        a = BainAdapter("bain", cfg, page_delay=0)
        a.http = FakeHttp({a.url(n): response for n, response in enumerate(responses)})
        return a

    def test_page_number_resume_and_all_locations(self, _):
        a = self.adapter([page(0, 10, 13), page(10, 13, 13)], max_pages_per_run=1)
        first = a.fetch()
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor["page"], 1)
        self.assertEqual(first.postings[0].location, "London | Bengaluru")
        final = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(final.complete)
        self.assertEqual([p.job_id for p in final.postings], ["10", "11", "12"])

    def test_normal_frontend_referer(self, _):
        class HTTP:
            def get_json(self, url, headers):
                self.headers = headers
                return page(0, 0, 0)
        a = BainAdapter("bain", {}, http=HTTP())
        self.assertTrue(a.fetch().complete)
        self.assertEqual(a.http.headers, {"Referer": REFERER})
        self.assertIn(f"results={DEFAULT_PAGE_SIZE}", a.url(0))

    def test_default_page_size_fetches_current_inventory_without_boundaries(self, _):
        a = BainAdapter("bain", {}, page_delay=0)
        a.http = FakeHttp({a.url(0): page(0, 326, 326)})
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 326)
        self.assertEqual(a.http.requests, 1)

    def test_page_size_is_bounded(self, _):
        for size in (True, 0, 501, "400"):
            with self.subTest(size=size), self.assertRaises(AdapterError):
                BainAdapter("bain", {"page_size": size}, page_delay=0).fetch()

    def test_throttle_retains_progress_including_resumed_first_request(self, _):
        for status in (403, 429):
            a = self.adapter([page(0, 10, 13), HttpStatusError("url", status, 120)])
            result = a.fetch()
            self.assertEqual(result.cursor["page"], 1)
            self.assertEqual(result.throttled.retry_after, 120)
            again = a.fetch(FetchContext(cursor=result.cursor))
            self.assertEqual(again.cursor, result.cursor)
            self.assertEqual(again.postings, [])

    def test_count_change_or_overlap_restarts_without_closures(self, _):
        for tail in (page(10, 14, 14), page(0, 3, 13)):
            result = self.adapter([page(0, 10, 13), tail]).fetch()
            self.assertFalse(result.complete)
            self.assertTrue(result.restart)
            self.assertIsNone(result.cursor)
            self.assertEqual(result.postings, [])

    def test_short_page_missing_metadata_and_unsafe_link_fail(self, _):
        bad_rows = [row(1, Location=None), row(1, JobTitle=""),
                    row(1, Link="https://elsewhere.test/?jobid=1"),
                    row(1, Link="/careers/find-a-role/position/?jobid=2")]
        payloads = [{}, page(0, 0, 1), page(0, 1, 2),
                    {"results": [], "totalResults": True},
                    {"results": [row(1), row(1)], "totalResults": 2}]
        payloads += [{"results": [r], "totalResults": 1} for r in bad_rows]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(AdapterError):
                self.adapter([payload]).fetch()

    def test_explicit_unknown_location_retained_but_not_india_eligible(self, _):
        job = row(1, Location=[], Link="/careers/work-with-us/internships-programs/program/")
        result = self.adapter([{"results": [job], "totalResults": 1}]).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings[0].location, "")
        self.assertFalse(Filter(location=["india", "bengaluru"]).matches(result.postings[0]))


if __name__ == "__main__":
    unittest.main()


class BainProgrammePages(unittest.TestCase):
    """Bain adds new KINDS of evergreen programme page, not just new jobs.

    On 2026-10-11 one row, "Undergraduate exploratory programs" (id 2791789),
    appeared under /careers/recruiting/ and failed the whole 298-row sweep for
    days. Accepting the prefix is right; accepting any Bain path is not.
    """

    def adapter(self, link):
        a = BainAdapter("bain", {"page_size": 10}, page_delay=0)
        a.http = FakeHttp({a.url(0): {"results": [row(1, Link=link)], "totalResults": 1}})
        return a

    @patch.object(BainAdapter, "check_robots")
    def test_recruiting_programme_page_is_accepted(self, _):
        a = self.adapter("/careers/recruiting/undergraduate-exploratory-programs/")
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings[0].url,
                         "https://www.bain.com/careers/recruiting/undergraduate-exploratory-programs/")

    @patch.object(BainAdapter, "check_robots")
    def test_internship_programme_page_still_accepted(self, _):
        a = self.adapter("/careers/work-with-us/internships-programs/associate-consultant-internship/")
        self.assertTrue(a.fetch().complete)

    @patch.object(BainAdapter, "check_robots")
    def test_an_unrelated_bain_path_is_still_refused(self, _):
        for link in ("/careers/", "/about-us/", "/careers/recruiting", "/vacancies/x/"):
            with self.subTest(link=link):
                self.assertRaises(AdapterError, self.adapter(link).fetch)

    @patch.object(BainAdapter, "check_robots")
    def test_a_position_link_must_still_carry_its_own_jobid(self, _):
        a = self.adapter("/careers/find-a-role/position/?jobid=999")
        self.assertRaises(AdapterError, a.fetch)
