import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.mckinsey import McKinseyAdapter, URL
from watcher.filters import Filter
from watcher.http import HttpStatusError


def row(n=1, **changes):
    return dict({"jobID": str(n), "title": "Software Engineer",
                 "friendlyURL": f"software-engineer-{n}",
                 "jobApplyURL": f"https://mckinsey.avature.net/careers/ApplicationMethods?folderId={n}",
                 "cities": ["London", "Bengaluru"], "countries": ["United Kingdom", "India"]},
                **changes)


def payload(rows, **changes):
    return dict({"docs": rows, "numFound": len(rows), "httpStatus": "OK"}, **changes)


@patch.object(McKinseyAdapter, "check_robots")
class McKinseySafety(unittest.TestCase):
    def adapter(self, data):
        return McKinseyAdapter("mckinsey", {}, http=FakeHttp({URL: data}), page_delay=0)

    def test_complete_single_request_and_secondary_india_location(self, robots):
        a = self.adapter(payload([row(n) for n in range(595)]))
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 595)
        self.assertEqual(a.http.requests, 1)
        robots.assert_called_once_with(URL)
        p = result.postings[0]
        self.assertEqual(p.location, "London, United Kingdom | Bengaluru, India")
        self.assertTrue(Filter(location=["india"]).matches(p))
        self.assertEqual(p.posted_at, "")
        self.assertEqual(p.req_id, "0")

    def test_malformed_or_truncated_inventory_never_completes(self, _):
        for data in ({}, [], payload([], numFound=True), payload([], numFound=-1),
                     payload([row()], numFound=2), payload([row()], numFound=801),
                     payload([row(), row()]), payload([row()], httpStatus="ERROR")):
            with self.subTest(data=data), self.assertRaises(AdapterError):
                self.adapter(data).fetch()

    def test_identity_and_location_corruption_rejected(self, _):
        for changes in ({"cities": ["London"]}, {"countries": ["", "India"]},
                        {"countries": "India"}, {"cities": [], "countries": []},
                        {"title": " "}, {"jobID": True},
                        {"friendlyURL": "software-engineer-2"},
                        {"friendlyURL": "../software-engineer-1"},
                        {"jobApplyURL": "https://elsewhere.test/careers/ApplicationMethods?folderId=1"},
                        {"jobApplyURL": "https://mckinsey.avature.net/careers/ApplicationMethods?folderId=2"}):
            with self.subTest(changes=changes), self.assertRaises(AdapterError):
                self.adapter(payload([row(**changes)])).fetch()

    def test_counted_empty_and_non_india_inventory(self, _):
        self.assertTrue(self.adapter(payload([])).fetch().complete)
        p = self.adapter(payload([row(cities=["London"], countries=["United Kingdom"])])).fetch().postings[0]
        self.assertFalse(Filter(location=["india", "bengaluru"]).matches(p))

    def test_unexpected_cursor_and_throttle_cannot_close_jobs(self, _):
        a = self.adapter(payload([]))
        with self.assertRaises(AdapterError):
            a.fetch(FetchContext(cursor={"page": 2}))
        self.assertEqual(a.http.requests, 0)
        with self.assertRaises(HttpStatusError):
            self.adapter(HttpStatusError(URL, 429)).fetch()


if __name__ == "__main__":
    unittest.main()
