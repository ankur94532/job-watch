import json
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

from watcher.adapters.base import AdapterError
from watcher.adapters.meta import (GRAPHQL, LOCATION_DOC_ID, MetaAdapter,
                                   PAGE, SEARCH_DOC_ID)


PAGE_HTML = '''
<script>["LSD",[],{"token":"honest-token"},323]</script>
<script>["SiteData",[],{"haste_session":"session","client_revision":12,
"__spin_r":12,"__spin_b":"trunk","__spin_t":34,"hsi":"56"},317]</script>
'''

_MISSING = object()


def locations(rows=_MISSING):
    if rows is _MISSING:
        rows = [
            {"id": "bangalore", "location_display_name": "Bangalore, India",
             "country": "India"},
            {"id": "hyderabad", "location_display_name": "Hyderabad, India",
             "country": "India"},
            {"id": "london", "location_display_name": "London, UK", "country": "UK"},
        ]
    return {"data": {"job_search_filters": {"locations": rows}}}


def jobs(rows, featured=None):
    if featured is None:
        featured = [{"id": "999", "title": "Unrelated featured job",
                     "locations": ["London, UK"]}]
    return {"data": {"job_search_with_featured_jobs": {
        "all_jobs": rows,
        "featured_jobs": featured,
    }}}


def row(job_id="1", title="ASIC Engineer", locations=None):
    return {"id": job_id, "title": title,
            "locations": ["Bangalore, India"] if locations is None else locations,
            "teams": ["Infrastructure"], "sub_teams": ["Engineering"]}


class MetaHttp:
    def __init__(self, location_payload=_MISSING, search_payload=_MISSING):
        self.location_payload = (locations() if location_payload is _MISSING
                                 else location_payload)
        self.search_payload = (jobs([row(), row("2", locations=[])])
                               if search_payload is _MISSING else search_payload)
        self.calls = []
        self.requests = 0

    def get_text(self, url, headers=None):
        self.calls.append(("GET", url, None, headers))
        self.requests += 1
        return PAGE_HTML

    def request(self, method, url, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        self.requests += 1
        form = parse_qs(data.decode())
        payload = self.location_payload if form["doc_id"] == [LOCATION_DOC_ID] else self.search_payload
        return json.dumps(payload).encode(), {}


@patch.object(MetaAdapter, "check_robots")
class MetaSafety(unittest.TestCase):
    def test_dynamic_india_filter_and_locationless_match(self, _):
        http = MetaHttp()
        result = MetaAdapter("meta", {}, http=http, page_delay=0).fetch()
        self.assertTrue(result.complete)
        self.assertEqual([posting.job_id for posting in result.postings], ["1", "2"])
        self.assertEqual(result.postings[1].location, "India")
        self.assertEqual(result.postings[0].url,
                         "https://www.metacareers.com/profile/job_details/1/")
        self.assertNotIn("999", [posting.job_id for posting in result.postings])
        self.assertEqual(http.requests, 3)
        search_call = http.calls[-1]
        self.assertEqual(search_call[1], GRAPHQL)
        form = parse_qs(search_call[2].decode())
        self.assertEqual(form["doc_id"], [SEARCH_DOC_ID])
        variables = json.loads(form["variables"][0])
        self.assertEqual(variables["search_input"]["offices"],
                         ["Bangalore, India", "Hyderabad, India"])
        self.assertFalse(variables["isLoggedIn"])
        self.assertEqual(search_call[3]["Referer"], PAGE)

    def test_invalid_context_and_query_shapes_fail(self, _):
        a = MetaAdapter("meta", {}, http=MetaHttp(), page_delay=0)
        a.http.get_text = lambda *args, **kwargs: "<html></html>"
        with self.assertRaises(AdapterError):
            a.fetch()
        for location_payload, search_payload in (
                ({}, jobs([row()])),
                (locations([]), jobs([row()])),
                (locations(), {}),
                (locations(), {"data": {"job_search_with_featured_jobs": {
                    "all_jobs": [row()]}}}),
                (locations(), {"errors": [{"message": "stale document"}]})):
            with self.subTest(location_payload=location_payload, search_payload=search_payload):
                with self.assertRaises(AdapterError):
                    MetaAdapter("meta", {}, http=MetaHttp(location_payload, search_payload),
                                page_delay=0).fetch()

    def test_malformed_duplicate_and_filter_mismatch_fail(self, _):
        bad_rows = [
            [row("x")],
            [row(title="")],
            [row(locations=None)],
            [row(locations=["London, UK"])],
            [row(), row()],
        ]
        # `None` normally asks the helper for its default, so set it explicitly.
        bad_rows[2][0]["locations"] = None
        for rows in bad_rows:
            with self.subTest(rows=rows), self.assertRaises(AdapterError):
                MetaAdapter("meta", {}, http=MetaHttp(search_payload=jobs(rows)),
                            page_delay=0).fetch()

    def test_india_featured_jobs_are_included_and_deduplicated(self, _):
        featured = [row("3", title="Featured Engineer", locations=["Remote, India"]),
                    row("1"),
                    row("999", locations=["London, UK"])]
        result = MetaAdapter(
            "meta", {}, http=MetaHttp(search_payload=jobs([row()], featured)),
            page_delay=0).fetch()
        self.assertEqual([posting.job_id for posting in result.postings], ["1", "3"])
        self.assertEqual(result.postings[1].location, "Remote, India")

    def test_malformed_featured_job_fails_even_when_outside_india(self, _):
        featured = [row("x", locations=["London, UK"])]
        with self.assertRaises(AdapterError):
            MetaAdapter(
                "meta", {}, http=MetaHttp(search_payload=jobs([row()], featured)),
                page_delay=0).fetch()

    def test_query_headers_and_context_are_frontend_values(self, _):
        http = MetaHttp()
        MetaAdapter("meta", {}, http=http, page_delay=0).fetch()
        form = parse_qs(http.calls[1][2].decode())
        self.assertEqual(form["lsd"], ["honest-token"])
        self.assertEqual(form["__hs"], ["session"])
        self.assertEqual(form["jazoest"],
                         ["2" + str(sum(ord(char) for char in "honest-token"))])
        self.assertEqual(http.calls[1][3]["X-FB-LSD"], "honest-token")


if __name__ == "__main__":
    unittest.main()
