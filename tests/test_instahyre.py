import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.instahyre import PAGE, InstahyreAdapter
from watcher.http import HttpStatusError

EMPLOYER, EMPLOYER_ID = "Blinkit", 333


def employer(**changes):
    return dict({"id": EMPLOYER_ID, "company_name": EMPLOYER}, **changes)


def row(n, **changes):
    return dict({"id": n, "title": "Software Engineer", "locations": "Gurgaon",
                 "public_url": f"https://www.instahyre.com/job-{n}-software-engineer-at-blinkit-gurgaon/",
                 "employer": employer()}, **changes)


def page(start, stop, total):
    # ids are 1-based: Instahyre has no job 0, and the adapter refuses one
    return {"objects": [row(n + 1) for n in range(start, stop)], "meta": {"total_count": total}}


@patch.object(InstahyreAdapter, "check_robots")
class InstahyreSafety(unittest.TestCase):
    def adapter(self, responses, **cfg):
        cfg.setdefault("employer", EMPLOYER)
        cfg.setdefault("employer_id", EMPLOYER_ID)
        a = InstahyreAdapter("blinkit-instahyre", cfg, page_delay=0)
        a.http = FakeHttp({a.url(n * PAGE): r for n, r in enumerate(responses)})
        return a

    def test_single_page_sweep_reports_declared_count(self, _):
        a = self.adapter([page(0, 4, 4)])
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.declared, 4)
        self.assertEqual([p.job_id for p in result.postings], ["1", "2", "3", "4"])
        self.assertEqual(result.postings[0].location, "Gurgaon")
        self.assertEqual(result.postings[0].posted_at, "")

    def test_offset_pagination_resumes(self, _):
        a = self.adapter([page(0, PAGE, 48), page(PAGE, 48, 48)], max_pages_per_run=1)
        first = a.fetch()
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor["offset"], PAGE)
        final = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(final.complete)
        self.assertEqual(len(final.postings), 48 - PAGE)

    def test_multi_location_row_is_split(self, _):
        a = self.adapter([{"objects": [row(1, locations="Bangalore,Gurgaon")],
                           "meta": {"total_count": 1}}])
        self.assertEqual(a.fetch().postings[0].location, "Bangalore | Gurgaon")

    def test_work_from_home_marks_remote(self, _):
        a = self.adapter([{"objects": [row(1, locations="Work From Home")],
                           "meta": {"total_count": 1}}])
        self.assertTrue(a.fetch().postings[0].remote)

    def test_ignored_filter_returning_another_employer_is_refused(self, _):
        """`company=` instead of `companies=` returns the whole site unfiltered."""
        a = self.adapter([{"objects": [row(1), row(2, employer=employer(
            id=38, company_name="Flipkart"))], "meta": {"total_count": 2}}])
        with self.assertRaises(AdapterError) as e:
            a.fetch()
        self.assertIn("Flipkart", str(e.exception))

    def test_matching_name_but_wrong_employer_id_is_refused(self, _):
        a = self.adapter([{"objects": [row(1, employer=employer(id=999))],
                           "meta": {"total_count": 1}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_empty_known_employer_needs_no_numeric_id(self, _):
        a = self.adapter([{"objects": [], "meta": {"total_count": 0}}], employer_id=None)
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings, [])

    def test_missing_configured_id_learns_one_consistent_row_id(self, _):
        a = self.adapter([page(0, 2, 2)], employer_id=None)
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["1", "2"])

    def test_missing_configured_id_refuses_multiple_row_ids(self, _):
        a = self.adapter([{"objects": [row(1), row(2, employer=employer(id=999))],
                           "meta": {"total_count": 2}}], employer_id=None)
        with self.assertRaises(AdapterError) as e:
            a.fetch()
        self.assertIn("multiple employer ids", str(e.exception))

    def test_learned_employer_id_survives_resume(self, _):
        a = self.adapter([page(0, PAGE, 36), page(PAGE, 36, 36)],
                         employer_id=None, max_pages_per_run=1)
        first = a.fetch()
        self.assertEqual(first.cursor["employer_ids"], [EMPLOYER_ID])
        second_page = page(PAGE, 36, 36)
        second_page["objects"][0]["employer"] = employer(id=999)
        a.http = FakeHttp({a.url(PAGE): second_page})
        with self.assertRaises(AdapterError) as e:
            a.fetch(FetchContext(cursor=first.cursor))
        self.assertIn("multiple employer ids", str(e.exception))

    def test_link_must_belong_to_the_job(self, _):
        a = self.adapter([{"objects": [row(1, public_url="https://example.com/job-1-x/")],
                           "meta": {"total_count": 1}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_link_id_must_match_row_id(self, _):
        a = self.adapter([{"objects": [row(1, public_url=(
            "https://www.instahyre.com/job-2-software-engineer-at-blinkit-gurgaon/"))],
            "meta": {"total_count": 1}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_short_page_is_refused_rather_than_completing(self, _):
        a = self.adapter([{"objects": [row(1)], "meta": {"total_count": 9}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_missing_count_is_refused(self, _):
        a = self.adapter([{"objects": [], "meta": {}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_duplicate_ids_in_one_page_refused(self, _):
        a = self.adapter([{"objects": [row(1), row(1)], "meta": {"total_count": 2}}])
        self.assertRaises(AdapterError, a.fetch)

    def test_count_change_mid_sweep_restarts(self, _):
        a = self.adapter([page(0, PAGE, 48), page(PAGE, 40, 40)], max_pages_per_run=1)
        first = a.fetch()
        second = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.restart)
        self.assertEqual(second.postings, [])

    def test_overlapping_page_restarts(self, _):
        a = self.adapter([page(0, PAGE, 48), page(0, 13, 48)], max_pages_per_run=1)
        first = a.fetch()
        second = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.restart)

    def test_throttle_keeps_rows_already_paid_for(self, _):
        a = self.adapter([page(0, PAGE, 48)], max_pages_per_run=2)
        a.http = FakeHttp({a.url(0): page(0, PAGE, 48),
                           a.url(PAGE): HttpStatusError("url", 429)})
        result = a.fetch()
        self.assertFalse(result.complete)
        self.assertIsNotNone(result.throttled)
        self.assertEqual(len(result.postings), PAGE)
        self.assertEqual(result.cursor["offset"], PAGE)

    def test_empty_inventory_is_complete_not_an_error(self, _):
        a = self.adapter([{"objects": [], "meta": {"total_count": 0}}])
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings, [])

    def test_bad_cursor_is_refused(self, _):
        a = self.adapter([page(0, 4, 4)])
        self.assertRaises(AdapterError, a.fetch,
                          FetchContext(cursor={"offset": 5, "total": 9, "ids": ["1"]}))

    def test_employer_id_must_be_configured_as_an_integer(self, _):
        a = InstahyreAdapter("x", {"employer": EMPLOYER, "employer_id": "333"})
        self.assertRaises(AdapterError, a.employer_id)
        # and the sweep refuses before sending a request, not halfway through
        self.assertRaises(AdapterError, a.fetch)

    def test_uses_the_companies_filter_not_company(self, _):
        a = self.adapter([page(0, 0, 0)])
        self.assertIn("companies=Blinkit", a.url(0))
        self.assertIn("limit=%d" % PAGE, a.url(0))


if __name__ == "__main__":
    unittest.main()
