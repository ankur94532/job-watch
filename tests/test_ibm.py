import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError
from watcher.adapters.ibm import IBMAdapter


def row(n, **changes):
    base = {
        "resultnum": n,
        "id": ("%064x" % (n + 1)),
        "title": "Software Engineer",
        "url": f"https://careers.ibm.com/careers/JobDetail?jobId={1000 + n}",
        "docattributes": [
            {"field_keyword_05": "India"},
            {"field_keyword_19": "Bangalore, IN"},
            {"field_keyword_17": "Hybrid"},
            {"dcdate": "2026-09-20"},
        ],
    }
    base.update(changes)
    return base


def result(rows, total=None, **search_changes):
    total = len(rows) if total is None else total
    search = {"totalresults": total, "startindex": 0, "numresults": total,
              "searchresultlist": rows}
    search.update(search_changes)
    return {"resultset": {
        "searchquery": {"appid": "careers", "collection": "ibmcom",
                        "sources": "genesis-prod", "userquery": "",
                        "filter": "field_keyword_05:India"},
        "searchresults": search,
    }}


@patch.object(IBMAdapter, "check_robots")
class IBMSafety(unittest.TestCase):
    def adapter(self, payload):
        adapter = IBMAdapter("ibm", {}, page_delay=0)
        adapter.http = FakeHttp({adapter.url(): payload})
        return adapter

    def test_complete_one_request_inventory_and_fields(self, _):
        remote = row(1, docattributes=[{"field_keyword_05": "India"},
            {"field_keyword_19": "Hyderabad, IN"}, {"field_keyword_17": "Remote"},
            {"dcdate": "2026-09-19"}])
        fetched = self.adapter(result([row(0), remote])).fetch()
        self.assertTrue(fetched.complete)
        self.assertEqual([posting.job_id for posting in fetched.postings], ["1000", "1001"])
        self.assertEqual(fetched.postings[0].location, "Bangalore, IN | India")
        self.assertTrue(fetched.postings[1].remote)
        self.assertEqual(fetched.postings[1].posted_at, "2026-09-19")
        self.assertEqual(self.adapter(result([])).fetch().postings, [])

    def test_count_query_and_source_identity_are_exact(self, _):
        bad = [
            {},
            {"resultset": {}},
            result([row(0)], total=2),
            result([row(0)], startindex=1),
            result([row(0)], numresults=0),
        ]
        for key, value in (("appid", "other"), ("collection", "other"),
                           ("sources", ""), ("userquery", "x"),
                           ("filter", "field_keyword_05:France")):
            payload = result([row(0)])
            payload["resultset"]["searchquery"][key] = value
            bad.append(payload)
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(AdapterError):
                self.adapter(payload).fetch()

    def test_job_country_metadata_and_url_fail_closed(self, _):
        bad_rows = [
            row(0, resultnum=1),
            row(0, id="bad"),
            row(0, title=""),
            row(0, url="https://example.com/careers/JobDetail?jobId=1000"),
            row(0, url="https://careers.ibm.com/careers/JobDetail?jobId=not-numeric"),
            row(0, docattributes=[]),
            row(0, docattributes=[{"field_keyword_05": "France"},
                {"field_keyword_19": "Paris"}, {"field_keyword_17": "Hybrid"},
                {"dcdate": "2026-09-20"}]),
            row(0, docattributes=[{"field_keyword_05": "India"},
                {"field_keyword_19": ""}, {"field_keyword_17": "Hybrid"},
                {"dcdate": "2026-09-20"}]),
            row(0, docattributes=[{"field_keyword_05": "India"},
                {"field_keyword_19": "Pune, IN"}, {"field_keyword_17": "Hybrid"},
                {"dcdate": "20 September"}]),
        ]
        for bad_row in bad_rows:
            with self.subTest(row=bad_row), self.assertRaises(AdapterError):
                self.adapter(result([bad_row])).fetch()

    def test_duplicate_job_or_search_identity_is_rejected(self, _):
        duplicate_job = row(1, url="https://careers.ibm.com/careers/JobDetail?jobId=1000")
        duplicate_search = row(1, id=row(0)["id"])
        for rows in ([row(0), duplicate_job], [row(0), duplicate_search]):
            with self.subTest(rows=rows), self.assertRaises(AdapterError):
                self.adapter(result(rows)).fetch()

    def test_conditional_cache_is_forwarded(self, _):
        adapter = IBMAdapter("ibm", {}, page_delay=0)
        adapter.http = FakeHttp({adapter.url(): result([row(0)])}, etag='"ibm"')
        cache = {}
        from watcher.adapters.base import FetchContext
        adapter.fetch(FetchContext(cache=cache))
        self.assertEqual(cache, {"etag": '"ibm"'})


if __name__ == "__main__":
    unittest.main()
