import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.maersk import API, MaerskAdapter, PAGE
from watcher.http import HttpStatusError


KEY = "public-client-key"


def row(n, **changes):
    external_id = f"Software-Engineer_R{100000 + n}"
    return dict({
        "Title": "Software Engineer",
        "PostedDT": "2026-09-20T07:00:00+00:00",
        "Url": ("https://maersk.wd3.myworkdayjobs.com/Maersk_Careers/job/"
                f"India-Bengaluru/{external_id}"),
        "ExternalId": external_id,
        "Platform": "Workday",
        "Brand": "Maersk",
        "Country": "India",
        "City": "Bengaluru",
        "NewCity": None,
        "JobFlexibility": "Site Based",
    }, **changes)


def result(start, stop, total):
    return {"HasError": False, "ResultCount": total, "ResultsPerPage": PAGE,
            "Results": [row(n) for n in range(start, stop)]}


@patch.object(MaerskAdapter, "check_robots")
class MaerskSafety(unittest.TestCase):
    def adapter(self, responses, **cfg):
        cfg.setdefault("consumer_key", KEY)
        cfg.setdefault("country", "India")
        cfg.setdefault("max_pages_per_run", 100)
        adapter = MaerskAdapter("maersk", cfg, page_delay=0)
        adapter.http = FakeHttp({adapter.url(page): response
                                 for page, response in enumerate(responses)})
        return adapter

    def test_complete_counted_inventory_and_fields(self, _):
        missing_city = row(10, City=None, NewCity="")
        pages = [result(0, 10, 11),
                 {"HasError": False, "ResultCount": 11, "ResultsPerPage": PAGE,
                  "Results": [missing_city]}]
        fetched = self.adapter(pages).fetch()
        self.assertTrue(fetched.complete)
        self.assertEqual(len(fetched.postings), 11)
        self.assertEqual(fetched.postings[0].job_id, "R100000")
        self.assertEqual(fetched.postings[0].req_id, "R100000")
        self.assertEqual(fetched.postings[0].location, "Bengaluru | India")
        self.assertEqual(fetched.postings[-1].location, "India")
        self.assertEqual(fetched.postings[0].posted_at, "2026-09-20T07:00:00+00:00")

        apmt = row(1, Url=("https://maersk.wd3.myworkdayjobs.com/APMT_Careers/job/"
                          "India-Bengaluru/Software-Engineer_R100001"))
        self.assertEqual(self.adapter([{"HasError": False, "ResultCount": 1,
            "ResultsPerPage": PAGE, "Results": [apmt]}]).fetch().postings[0].job_id,
            "R100001")

    def test_page_number_resume_and_public_consumer_key(self, _):
        class HTTP:
            def __init__(self):
                self.calls = []

            def get_json(self, url, headers):
                self.calls.append((url, headers))
                page = int(url.split("offset=")[1].split("&")[0])
                return result(page * PAGE, min((page + 1) * PAGE, 21), 21)

        adapter = MaerskAdapter("maersk", {
            "consumer_key": KEY, "country": "India", "max_pages_per_run": 1,
        }, http=HTTP(), page_delay=0)
        first = adapter.fetch()
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor["page"], 1)
        self.assertEqual(adapter.http.calls[0][1], {"Consumer-Key": KEY})
        second = adapter.fetch(FetchContext(cursor=first.cursor))
        third = adapter.fetch(FetchContext(cursor=second.cursor))
        self.assertTrue(third.complete)
        self.assertEqual([call[0].split("offset=")[1].split("&")[0]
                          for call in adapter.http.calls], ["0", "1", "2"])

    def test_count_change_or_page_overlap_restarts(self, _):
        changed = self.adapter([result(0, 10, 11), result(10, 12, 12)]).fetch()
        self.assertFalse(changed.complete)
        self.assertTrue(changed.restart)
        overlap = result(0, 10, 11)
        overlap["Results"] = [row(9)]
        overlap["ResultCount"] = 11
        adapter = self.adapter([])
        adapter.http = FakeHttp({adapter.url(1): overlap})
        resumed = adapter.fetch(FetchContext(cursor={"page": 1, "total": 11,
            "ids": [f"R{100000 + n}" for n in range(10)]}))
        self.assertTrue(resumed.restart)

    def test_throttle_retains_resume_point(self, _):
        adapter = self.adapter([result(0, 10, 11), HttpStatusError(API, 429, 120)])
        fetched = adapter.fetch()
        self.assertEqual(fetched.cursor["page"], 1)
        self.assertEqual(len(fetched.postings), 10)
        self.assertEqual(fetched.throttled.retry_after, 120)

    def test_invalid_cursor_and_config_fail_closed(self, _):
        for cursor in ({"page": -1, "ids": []}, {"page": 1, "total": 11, "ids": []},
                       {"page": 1, "total": 10,
                        "ids": [f"R{100000 + n}" for n in range(10)]}):
            with self.subTest(cursor=cursor), self.assertRaises(AdapterError):
                self.adapter([]).fetch(FetchContext(cursor=cursor))
        for cfg in ({"country": "india"}, {"max_pages_per_run": True},
                    {"max_pages_per_run": 0}, {"max_pages_per_run": 101}):
            with self.subTest(cfg=cfg), self.assertRaises(AdapterError):
                self.adapter([result(0, 0, 0)], **cfg).fetch()

    def test_bad_metadata_short_page_and_identity_fail_closed(self, _):
        bad_payloads = [
            {},
            {"HasError": True, "ResultCount": 0, "ResultsPerPage": PAGE, "Results": []},
            {"HasError": False, "ResultCount": True, "ResultsPerPage": PAGE, "Results": []},
            result(0, 9, 10),
            {"HasError": False, "ResultCount": 2, "ResultsPerPage": PAGE,
             "Results": [row(1), row(1)]},
        ]
        bad_rows = [
            row(1, Country="United States"),
            row(1, Brand="Other"),
            row(1, Platform="Other"),
            row(1, Title=""),
            row(1, PostedDT=""),
            row(1, Url="https://example.com/Software-Engineer_R100001"),
            row(1, Url=("https://maersk.wd3.myworkdayjobs.com/Unknown_Careers/job/"
                        "India-Bengaluru/Software-Engineer_R100001")),
            row(1, ExternalId="Software-Engineer_NOT-A-REQ"),
        ]
        bad_payloads += [{"HasError": False, "ResultCount": 1,
                          "ResultsPerPage": PAGE, "Results": [job]}
                         for job in bad_rows]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(AdapterError):
                self.adapter([payload]).fetch()


if __name__ == "__main__":
    unittest.main()
