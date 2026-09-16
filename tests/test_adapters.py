import unittest

from tests.fakes import FakeHttp, fixture
from watcher.adapters import FetchContext, build_adapter
from watcher.http import NotModified


class Greenhouse(unittest.TestCase):
    URL = "https://boards-api.greenhouse.io/v1/boards/rubrik/jobs?content=true"

    def test_parse(self):
        a = build_adapter("rubrik", {"ats": "greenhouse", "token": "rubrik"},
                          http=FakeHttp({self.URL: fixture("greenhouse_rubrik.json")}))
        ps = a.fetch().postings
        self.assertEqual(len(ps), 3)
        p = ps[0]
        self.assertEqual(p.ats, "greenhouse")
        self.assertTrue(p.job_id.isdigit())
        self.assertTrue(p.req_id)                       # requisition_id kept for referrals
        self.assertIn("India", p.location)
        self.assertIn("gh_jid=", p.url)
        self.assertIn("content", p.raw)

    def test_conditional_get(self):
        http = FakeHttp({self.URL: fixture("greenhouse_rubrik.json")}, etag='W/"abc"')
        a = build_adapter("rubrik", {"ats": "greenhouse", "token": "rubrik"}, http=http)
        ctx = FetchContext()
        a.fetch(ctx)
        self.assertEqual(ctx.cache, {"etag": 'W/"abc"'})
        with self.assertRaises(NotModified):
            a.fetch(FetchContext(cache=dict(ctx.cache)))


class Lever(unittest.TestCase):
    def test_parse(self):
        url = "https://api.lever.co/v0/postings/palantir?mode=json"
        a = build_adapter("palantir", {"ats": "lever", "token": "palantir"},
                          http=FakeHttp({url: fixture("lever_palantir.json")}))
        ps = a.fetch().postings
        self.assertEqual(len(ps), 3)
        self.assertTrue(ps[0].url.startswith("https://jobs.lever.co/palantir/"))
        self.assertRegex(ps[0].posted_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(ps[0].title)


class Ashby(unittest.TestCase):
    def test_parse(self):
        url = "https://api.ashbyhq.com/posting-api/job-board/ramp"
        a = build_adapter("ramp", {"ats": "ashby", "token": "ramp"}, http=FakeHttp({url: fixture("ashby_ramp.json")}))
        ps = a.fetch().postings
        self.assertEqual(len(ps), 3)
        self.assertEqual(ps[0].title, "Security Engineer, Cloud")      # whitespace trimmed
        self.assertIn("Remote (US)", ps[0].location)                   # secondary locations kept
        self.assertTrue(ps[0].remote)


class SmartRecruiters(unittest.TestCase):
    def url(self, offset, token="ServiceNow"):
        return f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}"

    def test_paginates_to_total(self):
        http = FakeHttp({
            self.url(0): fixture("smartrecruiters_servicenow_p0.json"),
            self.url(2): fixture("smartrecruiters_servicenow_p1.json"),
        })
        a = build_adapter("servicenow", {"ats": "smartrecruiters", "token": "ServiceNow"}, http=http)
        a.page_delay = 0
        ctx = FetchContext()
        ps = a.fetch(ctx).postings
        self.assertEqual(len(ps), 3)
        self.assertEqual(ps[0].url, f"https://jobs.smartrecruiters.com/ServiceNow/{ps[0].job_id}")
        self.assertTrue(ps[0].req_id)
        self.assertEqual(ctx.cache, {})     # multi-page board: no 304 shortcut

    def test_empty_board_is_empty(self):
        # SmartRecruiters answers 200 + empty list for any token, real or not.
        empty = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
        a = build_adapter("x", {"ats": "smartrecruiters", "token": "nope"},
                          http=FakeHttp({self.url(0, "nope"): empty}))
        self.assertEqual(a.fetch().postings, [])


if __name__ == "__main__":
    unittest.main()
