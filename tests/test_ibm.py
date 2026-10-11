import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.ibm import PAGE, IBMAdapter


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


def result(rows, total=None, start=0, **search_changes):
    total = len(rows) if total is None else total
    search = {"totalresults": total, "startindex": start, "numresults": len(rows),
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


@patch.object(IBMAdapter, "check_robots")
class IBMPaging(unittest.TestCase):
    """India outgrew IBM's own 1,000-row response ceiling on 2026-10-11.

    `nr` is truncated to 1,000 however large it is, so one request can no
    longer be complete. `fr` is the offset and `startindex` echoes it.
    """

    def adapter(self, pages):
        a = IBMAdapter("ibm", {}, page_delay=0)
        a.http = FakeHttp({a.url(off): body for off, body in pages.items()})
        return a

    def page(self, start, stop, total):
        # `resultnum` restarts at 0 on every page, so it indexes within the
        # response, not the whole inventory. Ids stay globally unique.
        return result([row(n, resultnum=i) for i, n in enumerate(range(start, stop))],
                      total=total, start=start)

    def test_two_pages_make_one_complete_sweep(self, _):
        a = self.adapter({0: self.page(0, PAGE, 1253), PAGE: self.page(PAGE, 1253, 1253)})
        res = a.fetch()
        self.assertTrue(res.complete)
        self.assertEqual((len(res.postings), res.declared), (1253, 1253))
        self.assertEqual(len({p.job_id for p in res.postings}), 1253)

    def test_single_page_inventory_still_completes(self, _):
        a = self.adapter({0: self.page(0, 12, 12)})
        self.assertTrue(a.fetch().complete)

    def test_offset_is_sent_and_echo_must_match(self, _):
        a = IBMAdapter("ibm", {}, page_delay=0)
        self.assertIn("fr=%d" % PAGE, a.url(PAGE))
        a.http = FakeHttp({a.url(0): self.page(0, PAGE, 1253),
                           a.url(PAGE): result([row(n, resultnum=i) for i, n
                                                in enumerate(range(PAGE, 1253))],
                                               total=1253, start=0)})   # wrong echo
        self.assertRaises(AdapterError, a.fetch)

    def test_total_changing_mid_sweep_restarts(self, _):
        a = self.adapter({0: self.page(0, PAGE, 1253), PAGE: self.page(PAGE, 1253, 1200)})
        res = a.fetch()
        self.assertTrue(res.restart)
        self.assertEqual(res.postings, [])

    def test_a_repeated_page_restarts_rather_than_double_counting(self, _):
        """Echoes the right offset but serves rows already seen."""
        repeat = result([row(n, resultnum=i) for i, n in enumerate(range(253))],
                        total=1253, start=PAGE)
        a = self.adapter({0: self.page(0, PAGE, 1253), PAGE: repeat})
        self.assertTrue(a.fetch().restart)

    def test_short_page_before_the_total_is_refused(self, _):
        a = self.adapter({0: result([row(n) for n in range(5)], total=1253, start=0)})
        self.assertRaises(AdapterError, a.fetch)

    def test_bad_cursor_is_refused(self, _):
        a = self.adapter({0: self.page(0, PAGE, 1253)})
        self.assertRaises(AdapterError, a.fetch,
                          FetchContext(cursor={"offset": 7, "total": 1253, "ids": []}))
