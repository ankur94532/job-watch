import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.cisco import CiscoAdapter


INDIA = "662e524adea41001f4d0bd5a1ddd0000"
LEGACY = "2101eee3ea9601b33c666b3203021730"
US = "1cf8ea09530d1001f4c77fd260d60000"


def job(n, location="Bangalore, India"):
    return {"title": f"Software Engineer {n}", "externalPath": f"/job/x/Role_R{n}",
            "locationsText": location, "postedOn": "Posted Today",
            "bulletFields": [f"R{n}"]}


def facets(*values):
    return {"total": 1000, "jobPostings": [], "facets": [{
        "facetParameter": "locationMainGroup",
        "values": [{"facetParameter": "locations", "descriptor": "Locations",
                    "values": list(values)}],
    }]}


def value(descriptor, location_id, count=1):
    return {"descriptor": descriptor, "id": location_id, "count": count}


class RoutingHttp:
    def __init__(self, filtered):
        self.filtered = list(filtered)
        self.calls = []

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload))
        if not payload["appliedFacets"]:
            return facets(value("Bangalore, India", INDIA, 2),
                          value("IND-BANGALORE", LEGACY),
                          value("San Jose, California, US", US, 5))
        return self.filtered.pop(0)


@patch.object(CiscoAdapter, "check_robots")
class CiscoSafety(unittest.TestCase):
    def adapter(self, responses):
        http = RoutingHttp(responses)
        return CiscoAdapter("cisco", {}, http=http, page_delay=0), http

    def test_derives_only_india_facets_and_completes_exact_inventory(self, robots):
        adapter, http = self.adapter([{"total": 2, "jobPostings": [job(1), job(2)]}])
        fetched = adapter.fetch()
        self.assertTrue(fetched.complete)
        self.assertEqual([posting.job_id for posting in fetched.postings], ["R1", "R2"])
        self.assertEqual(http.calls[1][1]["appliedFacets"],
                         {"locations": sorted([INDIA, LEGACY])})
        robots.assert_called_once()

    def test_resume_holds_the_original_dynamic_facets(self, robots):
        adapter, http = self.adapter([
            {"total": 21, "jobPostings": [job(n) for n in range(20)]},
        ])
        first = adapter.fetch(FetchContext(deadline=0))
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor["dynamic_locations"], sorted([INDIA, LEGACY]))

        # Workday deliberately re-reads the prior page on resume, then advances.
        resumed_http = RoutingHttp([
            {"total": 21, "jobPostings": [job(n) for n in range(20)]},
            {"total": 0, "jobPostings": [job(20)]},
        ])
        resumed = CiscoAdapter("cisco", {}, http=resumed_http, page_delay=0).fetch(
            FetchContext(cursor=first.cursor))
        self.assertTrue(resumed.complete)
        self.assertEqual([posting.job_id for posting in resumed.postings], ["R20"])
        self.assertTrue(all(call[1]["appliedFacets"] for call in resumed_http.calls))
        robots.assert_called_once()

    def test_exact_count_is_required(self, _):
        adapter, _ = self.adapter([{"total": 2, "jobPostings": [job(1)]}])
        with self.assertRaisesRegex(AdapterError, "incomplete sweep"):
            adapter.fetch()

    def test_bad_or_missing_location_facets_fail_closed(self, _):
        bad = [
            {},
            {"facets": []},
            facets(value("San Jose, California, US", US)),
            facets(value("Bangalore, India", "bad-id")),
            facets(value("Bangalore, India", INDIA, True)),
            facets(value("Bangalore, India", INDIA), value("Bangalore, India", LEGACY)),
        ]
        for payload in bad:
            adapter = CiscoAdapter("cisco", {}, page_delay=0)
            adapter.http = FakeHttp({adapter.api("Cisco_Careers") + "/jobs": payload})
            with self.subTest(payload=payload), self.assertRaises(AdapterError):
                adapter.fetch()

    def test_invalid_dynamic_facet_cursor_is_rejected_without_http(self, _):
        for locations in ([], ["bad"], [INDIA, INDIA], "not-a-list"):
            adapter = CiscoAdapter("cisco", {}, http=FakeHttp(), page_delay=0)
            cursor = {"site": 0, "offset": 20, "total": 21,
                      "site_ids": [f"R{n}" for n in range(20)],
                      "ids": [f"R{n}" for n in range(20)],
                      "dynamic_locations": locations}
            with self.subTest(locations=locations), self.assertRaises(AdapterError):
                adapter.fetch(FetchContext(cursor=cursor))


if __name__ == "__main__":
    unittest.main()
