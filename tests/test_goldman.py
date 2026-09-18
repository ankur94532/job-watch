"""Goldman Sachs Higher: paging, dedupe, coverage, and the job URL form."""

import unittest

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, build_adapter
from watcher.adapters import goldman as gs


def role(i, country="India", city="Bengaluru"):
    return {"roleId": f"{i}_GS_MID_CAREER", "jobTitle": f"Engineer {i}", "corporateTitle": "Associate",
            "locations": [{"city": city, "country": country}], "externalSource": {"sourceId": str(i)}}


def api(total, pages):
    """pages[n] = the items for pageNumber n."""
    def handler(method, url, payload, cache):
        n = payload["variables"]["searchQueryInput"]["page"]["pageNumber"]
        return {"data": {"roleSearch": {"totalCount": total, "items": pages.get(n, [])}}}
    return FakeHttp({gs.API: handler})


def adapter(http):
    return build_adapter("goldmansachs", {"ats": "gshigher"}, http=http, page_delay=0)


class Goldman(unittest.TestCase):
    def test_pages_until_the_total_and_links_the_role_by_source_id(self):
        pages = {0: [role(i) for i in range(100)], 1: [role(i) for i in range(100, 130)]}
        res = adapter(api(130, pages)).fetch()
        self.assertEqual(len(res.postings), 130)
        p = res.postings[0]
        # The roleId form ("0_GS_MID_CAREER") renders "Oops" in a browser; sourceId shows the role.
        self.assertEqual(p.url, "https://higher.gs.com/roles/0")
        self.assertEqual((p.job_id, p.location), ("0", "Bengaluru, India"))

    def test_the_same_role_on_two_pages_is_counted_once(self):
        pages = {0: [role(i) for i in range(100)], 1: [role(i) for i in range(95, 125)]}
        res = adapter(api(125, pages)).fetch()
        self.assertEqual(len({p.job_id for p in res.postings}), len(res.postings))

    def test_a_short_sweep_raises_instead_of_retiring_roles(self):
        pages = {0: [role(i) for i in range(100)], 1: [role(i) for i in range(100, 150)]}
        with self.assertRaisesRegex(AdapterError, "incomplete sweep"):
            adapter(api(962, pages)).fetch()

    def test_an_api_error_is_an_error_not_an_empty_board(self):
        http = FakeHttp({gs.API: {"data": None, "errors": [{"message": "bad input"}]}})
        with self.assertRaisesRegex(AdapterError, "roleSearch returned nothing"):
            adapter(http).fetch()


if __name__ == "__main__":
    unittest.main()
