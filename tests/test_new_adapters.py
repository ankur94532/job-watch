"""Eightfold, Amazon and Atlassian — the less uniform boards.

Fixtures are real responses recorded 2026-09-16; `count` / `hits` were edited
down to match the trimmed row sets (noted per test).
"""

import unittest

from tests.fakes import FakeHttp, fixture
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.adapters import eightfold as ef
from watcher.http import NotModified


class Eightfold(unittest.TestCase):
    CFG = {"ats": "eightfold", "host": "apply.careers.microsoft.com",
           "domain": "microsoft.com", "locations": ["India"]}

    def url(self, start):
        return ("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"
                f"&query=&location=India&start={start}&num=10")

    def board(self):
        return FakeHttp({
            self.url(0): fixture("eightfold_microsoft_p0.json"),   # count edited to 5
            # the trimmed first page holds 3 rows, so the next start is 3, not 10
            self.url(3): fixture("eightfold_microsoft_p1.json"),
        })

    def test_pages_ten_at_a_time_and_stops_on_count(self):
        http = self.board()
        res = build_adapter("microsoft", self.CFG, http=http, page_delay=0).fetch()
        self.assertTrue(res.complete)
        self.assertEqual(len(res.postings), 5)
        self.assertEqual(len(http.calls), 2)              # stops at start >= count, no third page
        p = res.postings[0]
        self.assertEqual(p.ats, "eightfold")
        self.assertTrue(p.req_id.isdigit())               # displayJobId, what a referral needs
        self.assertNotEqual(p.req_id, p.job_id)
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("India", p.location)
        self.assertTrue(p.url.startswith("https://apply.careers.microsoft.com/careers/job/"))

    def test_advances_by_rows_returned_not_by_page_size(self):
        """A short page must not skip the rows the server didn't send."""
        http = self.board()
        build_adapter("microsoft", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual([c[1] for c in http.calls], [self.url(0), self.url(3)])

    def test_empty_page_ends_a_location(self):
        http = FakeHttp({self.url(0): {"data": {"count": 900, "positions": []}}})
        res = build_adapter("microsoft", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(res.postings, [])
        self.assertEqual(len(http.calls), 1)

    def test_one_query_per_location_deduped(self):
        cfg = dict(self.CFG, locations=["India", "Hyderabad"])
        page = fixture("eightfold_microsoft_p0.json")
        page["data"]["count"] = 3
        hyd = ("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"
               "&query=&location=Hyderabad&start=0&num=10")
        http = FakeHttp({self.url(0): page, hyd: page})
        res = build_adapter("microsoft", cfg, http=http, page_delay=0).fetch()
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(len(res.postings), 3)            # same rows from both queries, kept once

    def test_page_cap(self):
        endless = {"data": {"count": 10**6, "positions": [
            {"id": 1, "name": "x", "locations": ["India"], "positionUrl": "/careers/job/1"}]}}
        http = FakeHttp({lambda m, u, p: True: endless})
        with self.assertRaises(AdapterError):
            build_adapter("microsoft", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(http.requests, ef.MAX_PAGES)


class EightfoldApplyFlavour(unittest.TestCase):
    """Netflix speaks the older /api/apply/v2 shape; Microsoft's host rejects it."""

    CFG = {"ats": "eightfold", "api": "apply", "host": "explore.jobs.netflix.net",
           "domain": "netflix.com", "locations": ["India"]}
    URL = ("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
           "&location=India&start=0&num=10")

    def test_reads_top_level_positions(self):
        http = FakeHttp({self.URL: fixture("eightfold_netflix_apply.json")})
        res = build_adapter("netflix", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        p = res.postings[0]
        self.assertTrue(p.req_id.startswith("JR"))          # display_job_id, not the numeric id
        self.assertNotEqual(p.req_id, p.job_id)
        self.assertIn("India", p.location)
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")   # from t_update epoch
        self.assertIn("/careers/job/", p.url)

    def test_flavour_picks_the_endpoint(self):
        a = build_adapter("netflix", self.CFG, http=FakeHttp({}), page_delay=0)
        self.assertIn("/api/apply/v2/jobs", a.url("India", 0))
        b = build_adapter("microsoft", dict(self.CFG, api="pcsx"), http=FakeHttp({}), page_delay=0)
        self.assertIn("/api/pcsx/search", b.url("India", 0))

    def test_unknown_flavour_is_rejected(self):
        from watcher.adapters import AdapterError
        a = build_adapter("x", dict(self.CFG, api="nonsense"), http=FakeHttp({}), page_delay=0)
        with self.assertRaises(AdapterError):
            a.fetch()


class Jibe(unittest.TestCase):
    CFG = {"ats": "jibe", "host": "careers.docusign.com", "locations": ["India"]}
    URL = "https://careers.docusign.com/api/jobs?limit=100&page=1&location=India"

    def test_parse_and_stop_on_total(self):
        http = FakeHttp({self.URL: fixture("jibe_docusign.json")})
        res = build_adapter("docusign", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        self.assertEqual(len(http.calls), 1)                # totalCount reached, no page 2
        p = res.postings[0]
        self.assertEqual(p.job_id, p.req_id)
        self.assertTrue(p.location)
        self.assertNotIn("description", p.raw)              # multi-KB field dropped
        # not apply_url: that one is an iCIMS login wall
        self.assertEqual(p.url, f"https://careers.docusign.com/careers-home/jobs/{p.job_id}")
        self.assertNotIn("icims", p.url)

    def test_pages_one_based(self):
        page1 = fixture("jibe_docusign.json")
        page1["totalCount"] = 6
        page2 = fixture("jibe_docusign.json")
        for i, j in enumerate(page2["jobs"]):
            j["data"]["req_id"] = f"9{i}"
        page2["totalCount"] = 6
        url2 = self.URL.replace("page=1", "page=2")
        http = FakeHttp({self.URL: page1, url2: page2})
        res = build_adapter("docusign", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 6)
        self.assertEqual([c[1] for c in http.calls], [self.URL, url2])


class Amazon(unittest.TestCase):
    def url(self, offset):
        return (f"https://www.amazon.jobs/en/search.json?result_limit=100&offset={offset}"
                "&sort=recent&country=IND")

    def test_parse_and_stop_on_hits(self):
        http = FakeHttp({self.url(0): fixture("amazon_india_p0.json")})   # hits edited to 3
        res = build_adapter("amazon", {"ats": "amazon"}, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        self.assertEqual(len(http.calls), 1)
        p = res.postings[0]
        self.assertTrue(p.job_id.isdigit())
        self.assertEqual(p.req_id, p.job_id)              # id_icims is the requisition number
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")   # "September 16, 2026" normalised
        self.assertTrue(p.url.startswith("https://www.amazon.jobs/en/jobs/"))
        self.assertTrue(p.location.startswith("IN,"))

    def test_country_filter_is_the_unbracketed_one(self):
        # country[]=IND is silently ignored by the API; assert we never send it.
        http = FakeHttp({self.url(0): fixture("amazon_india_p0.json")})
        build_adapter("amazon", {"ats": "amazon"}, http=http, page_delay=0).fetch()
        url = http.calls[0][1]
        self.assertIn("country=IND", url)
        self.assertNotIn("country%5B%5D", url)
        self.assertNotIn("country[]", url)

    def test_pauses_at_deadline_and_resumes(self):
        page = fixture("amazon_india_p0.json")
        page["hits"] = 6                                   # two pages of 3
        http = FakeHttp({self.url(0): page, self.url(3): page})
        a = build_adapter("amazon", {"ats": "amazon"}, http=http, page_delay=0)
        first = a.fetch(FetchContext(deadline=-1.0))       # deadline already passed
        self.assertFalse(first.complete)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(first.cursor["offset"], 3)
        second = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.complete)
        self.assertEqual([c[1] for c in http.calls], [self.url(0), self.url(3)])
        self.assertEqual(second.postings, [])              # same three rows, already seen

    def test_single_page_board_revalidates(self):
        http = FakeHttp({self.url(0): fixture("amazon_india_p0.json")}, etag='W/"az"')
        a = build_adapter("amazon", {"ats": "amazon"}, http=http, page_delay=0)
        ctx = FetchContext()
        a.fetch(ctx)
        self.assertEqual(ctx.cache, {"etag": 'W/"az"', "single_page": True})
        with self.assertRaises(NotModified):
            a.fetch(FetchContext(cache=dict(ctx.cache)))


class Atlassian(unittest.TestCase):
    URL = "https://www.atlassian.com/endpoint/careers/listings"

    def test_parse(self):
        http = FakeHttp({self.URL: fixture("atlassian_listings.json")})
        res = build_adapter("atlassian", {"ats": "atlassian"}, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        p = res.postings[0]
        self.assertTrue(p.job_id.isdigit())
        self.assertEqual(p.req_id, p.job_id)
        self.assertIn("India", p.location)
        self.assertIn("icims.com/jobs/", p.url)           # links to the real iCIMS posting
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(p.remote)                          # "Remote - India - Remote"

    def test_conditional_get(self):
        http = FakeHttp({self.URL: fixture("atlassian_listings.json")}, etag='W/"atl"')
        a = build_adapter("atlassian", {"ats": "atlassian"}, http=http, page_delay=0)
        ctx = FetchContext()
        a.fetch(ctx)
        self.assertEqual(ctx.cache, {"etag": 'W/"atl"'})
        with self.assertRaises(NotModified):
            a.fetch(FetchContext(cache=dict(ctx.cache)))


class Rippling(unittest.TestCase):
    URL = "https://api.rippling.com/platform/api/ats/v1/board/rippling/jobs"

    def test_parse(self):
        http = FakeHttp({self.URL: fixture("rippling_board.json")})
        res = build_adapter("rippling", {"ats": "rippling", "token": "rippling"},
                            http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 4)      # 5 rows, one job listed twice
        p = res.postings[0]
        self.assertIn("India", p.location)
        self.assertTrue(p.url.startswith("https://ats.rippling.com/rippling/jobs/"))
        self.assertEqual(p.posted_at, "")      # board publishes neither a date...
        self.assertEqual(p.req_id, "")         # ...nor a requisition number

    def test_rows_repeat_per_location_and_are_merged(self):
        """647 rows are 334 jobs; first-wins would hide the Bangalore location."""
        from watcher.filters import Filter
        http = FakeHttp({self.URL: fixture("rippling_board.json")})
        res = build_adapter("rippling", {"ats": "rippling", "token": "rippling"},
                            http=http, page_delay=0).fetch()
        multi = [p for p in res.postings if p.job_id == "multi-1"]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0].location, "San Francisco, CA | Bangalore, India")
        self.assertTrue(Filter(title=["software engineer"], location=["india"]).matches(multi[0]))

    def test_undated_postings_still_rank_and_render(self):
        from watcher import notify
        a = {"company": "rippling", "job_id": "u1", "title": "Software Engineer",
             "location": "Bangalore, India", "posted_at": "", "req_id": "",
             "url": "https://ats.rippling.com/rippling/jobs/u1", "first_seen": 1_800_000_000}
        text, keys = notify.compose(notify.TelegramSink("t", "c"), [a], now=1_800_000_060)[0]
        self.assertIn("Software Engineer", text)
        self.assertNotIn("🗓", text)           # no date line rather than an empty one
        self.assertEqual(keys, [("rippling", "u1")])


class Radancy(unittest.TestCase):
    """Intuit: no JSON API, so the sitemap feed is the source. Weaker, and typed as such."""

    URL = "https://jobs.intuit.com/sitemap.xml"

    def http(self):
        import os
        from tests.fakes import FIXTURES
        with open(os.path.join(FIXTURES, "radancy_intuit_sitemap.xml"), "rb") as fh:
            body = fh.read()

        class XmlHttp:
            calls = []
            requests = 0

            def request(self_, method, url, data=None, headers=None):
                self_.calls.append((method, url, None))
                self_.requests += 1
                return body, {}

        return XmlHttp()

    def test_parses_job_urls_and_skips_other_pages(self):
        http = self.http()
        res = build_adapter("intuit", {"ats": "radancy", "host": "jobs.intuit.com"},
                            http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)          # the non-job page is ignored
        p = res.postings[0]
        self.assertEqual(p.location, "Bengaluru")
        self.assertEqual(p.job_id, p.req_id)
        self.assertTrue(p.job_id.isdigit())
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(p.url.startswith("https://jobs.intuit.com/job/bengaluru/"))
        self.assertEqual(p.title, p.title.title())       # slug-derived, so title-cased

    def test_slug_titles_still_match_filters(self):
        from watcher.filters import Filter
        from watcher.adapters.radancy import unslug
        self.assertEqual(unslug("staff-software-engineer-traffic"), "Staff Software Engineer Traffic")
        f = Filter(title=["software engineer", "back end"], location=["bengaluru"])
        self.assertTrue(f.title_ok(unslug("senior-software-engineer-2")))
        self.assertTrue(f.title_ok(unslug("back-end-developer")))
        self.assertTrue(f.location_ok(unslug("bengaluru")))

    def test_empty_or_changed_sitemap_is_an_error_not_an_empty_board(self):
        from watcher.adapters import AdapterError

        class Empty:
            requests = 0

            def request(self, method, url, data=None, headers=None):
                return (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                        b'sitemap/0.9"><url><loc>https://jobs.intuit.com/</loc></url></urlset>'), {}

        with self.assertRaises(AdapterError):
            build_adapter("intuit", {"ats": "radancy", "host": "jobs.intuit.com"},
                          http=Empty(), page_delay=0).fetch()


class Oracle(unittest.TestCase):
    CFG = {"ats": "oracle", "host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "locations": ["India"]}

    def url(self, offset):
        return ("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                "?onlyData=true&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber=CX_1001,limit=200,offset={offset},"
                "sortBy=POSTING_DATES_DESC,location=India")

    def test_pages_to_total_and_parses(self):
        http = FakeHttp({self.url(0): fixture("oracle_jpmc_p0.json"),      # TotalJobsCount edited to 5
                         self.url(3): fixture("oracle_jpmc_p1.json")})
        res = build_adapter("jpmorganchase", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 5)
        self.assertEqual(len(http.calls), 2)
        p = res.postings[0]
        self.assertEqual(p.job_id, p.req_id)
        self.assertIn("India", p.location)
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(p.url.startswith(
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/"))

    def test_missing_expand_is_an_error_not_an_empty_board(self):
        """Without expand= the API returns a count and no list. That must not read as zero jobs."""
        from watcher.adapters import AdapterError
        http = FakeHttp({self.url(0): {"items": [{"TotalJobsCount": 314}]}})
        with self.assertRaises(AdapterError):
            build_adapter("jpmorganchase", self.CFG, http=http, page_delay=0).fetch()

    def test_warns_when_the_tenant_ignores_the_location(self):
        """Oracle's own site returns the whole global board for an unknown location."""
        from watcher.adapters import oracle as orc
        page = fixture("oracle_jpmc_p0.json")
        page["items"][0]["TotalJobsCount"] = 3
        for r in page["items"][0]["requisitionList"]:
            r["PrimaryLocation"] = "Abilene, TX, United States"
        cfg = dict(self.CFG, locations=["Bengaluru"])
        url = self.url(0).replace("location=India", "location=Bengaluru")
        with self.assertLogs(orc.log, "WARNING") as logs:
            build_adapter("oracle", cfg, http=FakeHttp({url: page}), page_delay=0).fetch()
        self.assertIn("probably does not know that location", "".join(logs.output))

    def test_location_filter_is_in_the_finder_pseudo_query(self):
        http = FakeHttp({self.url(0): fixture("oracle_jpmc_p0.json"),
                         self.url(3): fixture("oracle_jpmc_p1.json")})
        build_adapter("jpmorganchase", self.CFG, http=http, page_delay=0).fetch()
        self.assertIn("finder=findReqs;siteNumber=CX_1001", http.calls[0][1])
        self.assertIn("location=India", http.calls[0][1])


if __name__ == "__main__":
    unittest.main()


class Gem(unittest.TestCase):
    """jobs.gem.com — one unauthenticated GraphQL POST, no token of any kind.

    Fixture: the live `promptql` board 2026-09-18, trimmed to 3 of 6 rows.
    """

    CFG = {"ats": "gem", "board": "promptql"}
    API = "https://jobs.gem.com/api/public/graphql/batch"

    def board(self):
        return FakeHttp({self.API: fixture("gem_promptql.json")})

    def test_posts_the_board_id_and_reads_the_batched_response(self):
        http = self.board()
        res = build_adapter("hasura", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        method, url, payload = http.calls[0]
        self.assertEqual((method, url), ("POST", self.API))
        self.assertEqual(payload[0]["variables"], {"boardId": "promptql"})   # batch = a list

    def test_links_to_the_permalink_not_the_board(self):
        res = build_adapter("hasura", self.CFG, http=self.board(), page_delay=0).fetch()
        p = next(x for x in res.postings if "Bangalore" in x.title)
        self.assertEqual(p.title, "Forward Deployed Analyst, Bangalore")
        self.assertIn("Bengaluru", p.location)
        # extId is the permalink segment; the opaque `id` is only good for dedup
        self.assertEqual(p.url, f"https://jobs.gem.com/promptql/{p.req_id}")
        self.assertNotEqual(p.job_id, p.req_id)

    def test_raises_when_the_batch_shape_changes(self):
        http = FakeHttp({self.API: {"data": {}}})            # an object, not a batch list
        with self.assertRaises(AdapterError):
            build_adapter("hasura", self.CFG, http=http, page_delay=0).fetch()

    def test_raises_when_the_job_postings_field_disappears(self):
        http = FakeHttp({self.API: [{"data": {"oatsExternalJobPostings": {}}}]})
        with self.assertRaises(AdapterError):
            build_adapter("hasura", self.CFG, http=http, page_delay=0).fetch()


class Comeet(unittest.TestCase):
    """comeet.co/careers-api — the uid and token both come from the careers page.

    Fixture: ScyllaDB's live board 2026-09-18, trimmed to 3 of 8 rows, keeping
    the pair that is one role posted in two countries.
    """

    CFG = {"ats": "comeet", "company_uid": "E4.006", "token": "TESTTOKEN"}
    URL = "https://www.comeet.co/careers-api/2.0/company/E4.006/positions?token=TESTTOKEN"

    def test_reads_a_bare_list_and_keeps_the_publishers_permalink(self):
        http = FakeHttp({self.URL: fixture("comeet_scylladb.json")})
        res = build_adapter("scylladb", self.CFG, http=http, page_delay=0).fetch()
        self.assertEqual(len(res.postings), 3)
        p = res.postings[0]
        self.assertEqual(p.req_id, "FC.078")
        self.assertTrue(p.url.startswith("https://www.comeet.com/jobs/scylladb/E4.006/"))
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}$")

    def test_one_role_in_two_countries_stays_two_requisitions(self):
        http = FakeHttp({self.URL: fixture("comeet_scylladb.json")})
        res = build_adapter("scylladb", self.CFG, http=http, page_delay=0).fetch()
        same = [p for p in res.postings if p.job_id.startswith("FC.078")]
        self.assertEqual({p.job_id for p in same}, {"FC.078", "FC.078-75.10C"})
        self.assertEqual(len({p.location for p in same}), 2)   # distinct places to apply

    def test_304_propagates_rather_than_reading_as_an_empty_board(self):
        # The board sends ETags. A 304 must never look like "every job closed",
        # so it is left for the runner to catch, like every other adapter.
        http = FakeHttp({self.URL: NotModified()})
        with self.assertRaises(NotModified):
            build_adapter("scylladb", self.CFG, http=http, page_delay=0).fetch()

    def test_raises_when_the_token_stops_returning_a_list(self):
        http = FakeHttp({self.URL: {"error": "invalid token"}})
        with self.assertRaises(AdapterError):
            build_adapter("scylladb", self.CFG, http=http, page_delay=0).fetch()


class Quadeye(unittest.TestCase):
    """Their Zoho Recruit proxy. Fixture: live feed 2026-09-18, 3 of 26 rows.

    These exist because the permalink shipped wrong: it was built from
    `Job_Opening_ID` ("ZR_18_JOB") when the route keys on the Zoho record `id`,
    so every link 404'd. The site is an SPA that answers 200 on every path, so
    only a rendered page reveals a dead link -- which is why the URL SHAPE is
    pinned here rather than trusted to a status check.
    """

    CFG = {"ats": "quadeye"}
    URL = "https://www.quadeye.com/api/zoho-recruit/job-openings"

    def postings(self):
        http = FakeHttp({self.URL: fixture("quadeye_openings.json")})
        return build_adapter("quadeye", self.CFG, http=http, page_delay=0).fetch().postings

    def by_req(self, req):
        return next(p for p in self.postings() if p.req_id == req)

    def test_permalink_uses_the_record_id_never_the_job_opening_id(self):
        p = self.by_req("ZR_18_JOB")
        self.assertEqual(
            p.url,
            "https://www.quadeye.com/jobs/199893000000440465/"
            "systems-engineer-c-developer?source=CareerSite")
        # the exact bug that shipped: the ZR_ id must never appear in the URL
        self.assertNotIn("ZR_18_JOB", p.url)
        self.assertNotIn("/careers/", p.url)

    def test_slug_matches_their_rule_for_punctuation(self):
        # "C++" and " - " both collapse to a single dash, as their anchors do
        self.assertIn("/systems-engineer-c-developer?", self.by_req("ZR_18_JOB").url)
        self.assertIn("/operations-engineer?", self.by_req("ZR_12_JOB").url)

    def test_job_id_stays_the_req_id_so_the_url_fix_does_not_realert(self):
        # job_id is the dedup key. Changing it to the record id would have made
        # all 26 rows look new and fired a false alert burst.
        p = self.by_req("ZR_18_JOB")
        self.assertEqual(p.job_id, "ZR_18_JOB")
        self.assertEqual(p.job_id, p.req_id)

    def test_falls_back_to_the_board_rather_than_inventing_a_link(self):
        payload = {"data": [{"Job_Opening_ID": "ZR_99_JOB", "Posting_Title": "Ghost",
                             "City": "Gurugram", "Country": "India"}]}     # no record id
        http = FakeHttp({self.URL: payload})
        p = build_adapter("quadeye", self.CFG, http=http, page_delay=0).fetch().postings[0]
        self.assertEqual(p.url, "https://www.quadeye.com/jobs")
        self.assertNotIn("ZR_99_JOB", p.url)

    def test_raises_when_the_feed_loses_its_data_key(self):
        with self.assertRaises(AdapterError):
            build_adapter("quadeye", self.CFG, http=FakeHttp({self.URL: {"rows": []}}),
                          page_delay=0).fetch()
