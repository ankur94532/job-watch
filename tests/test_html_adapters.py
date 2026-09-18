"""HTML adapters against real pages saved on 2026-09-18 (tests/fixtures).

What matters here is less "does it parse" than "does it say so when it can't":
a changed layout must raise, a partial parse must show against the page's own
count, and an empty board must carry the page's own words for being empty.
"""

import unittest

from tests.fakes import FakeHttp, page
from watcher.adapters import AdapterError, build_adapter

HOME = "https://careers.media.net/"
BD = "https://careers.media.net/business-development"


def medianet(routes):
    return build_adapter("medianet", {"ats": "medianet"}, http=FakeHttp(routes), page_delay=0)


def skima(host, routes):
    return build_adapter("nykaa", {"ats": "skima", "host": host}, http=FakeHttp(routes), page_delay=0)


def empty_dept(method, url, payload):
    return url.startswith(HOME) and url not in (HOME, BD)


class MediaNet(unittest.TestCase):
    def routes(self, **over):
        r = {HOME: page("medianet_home.html"), BD: page("medianet_business_development.html"),
             empty_dept: page("medianet_engineering.html")}
        r.update(over)
        return r

    def test_departments_and_their_openings(self):
        res = medianet(self.routes()).fetch()
        self.assertEqual(res.declared, 7)                       # 1 + 4 + 1 + 1 across the departments
        titles = [p.title for p in res.postings]
        self.assertIn("Account Executive | East", titles)
        self.assertIn("Director of Sales | East & Midwest", titles)   # &#038; decoded
        self.assertEqual(len(res.postings), 4)                  # the other 3 departments say "none" here
        self.assertIsNone(res.empty_evidence)
        p = next(p for p in res.postings if p.title.startswith("Account Executive"))
        self.assertEqual(p.job_id, "business-development/account-executive-chicago")
        self.assertEqual(p.url, "https://careers.media.net/business-development/account-executive-chicago/")

    def test_only_departments_that_declare_openings_are_fetched(self):
        a = medianet(self.routes())
        a.fetch()
        fetched = [u for _, u, _ in a.http.calls]
        self.assertNotIn("https://careers.media.net/engineering", fetched)   # "0 Position"
        self.assertEqual(len(fetched), 1 + 4)                   # home + the four non-empty departments

    def test_location_comes_from_the_job_page(self):
        job = "https://careers.media.net/business-development/account-executive-chicago/"
        a = medianet(self.routes(**{job: page("medianet_job.html")}))
        p = next(p for p in a.fetch().postings if p.url == job)
        self.assertTrue(a.needs_enrich(p))
        a.enrich(p)
        self.assertEqual(p.location, "New York")

    def test_a_home_page_without_the_openings_section_is_a_layout_change(self):
        with self.assertRaisesRegex(AdapterError, "layout changed"):
            medianet(self.routes(**{HOME: "<html><body>We moved.</body></html>"})).fetch()

    def test_a_department_that_declares_openings_but_lists_none_is_a_layout_change(self):
        with self.assertRaisesRegex(AdapterError, "declares 4 but has no openings list"):
            medianet(self.routes(**{BD: "<html>redesigned</html>"})).fetch()

    def test_every_department_at_zero_is_evidence_of_an_empty_board(self):
        home = page("medianet_home.html")
        for n in ("1 Position", "4 Positions"):
            home = home.replace(">" + n + "<", ">0 Position<")
        res = medianet(self.routes(**{HOME: home})).fetch()
        self.assertEqual((len(res.postings), res.declared), (0, 0))
        self.assertIn("0 Position", res.empty_evidence)


class Skima(unittest.TestCase):
    def test_a_board_with_jobs(self):
        res = skima("mccafe.skima.ai", {"https://mccafe.skima.ai/": page("skima_with_jobs.html")}).fetch()
        self.assertEqual((len(res.postings), res.declared), (5, 5))    # "Showing 5 of 5 - Jobs"
        first = res.postings[0]
        self.assertEqual(first.title, "PHP developer - Facebook")
        self.assertEqual(first.location, "Surat")
        self.assertEqual(first.url, "https://mccafe.skima.ai/163ee726-baa4-4b27-b065-e7a9d89998d1")
        self.assertEqual([p.remote for p in res.postings], [False, True, True, False, False])
        self.assertIsNone(res.empty_evidence)

    def test_nykaa_today_is_empty_and_the_page_says_so(self):
        res = skima("careers.nykaa.com", {"https://careers.nykaa.com/": page("skima_nykaa_empty.html")}).fetch()
        self.assertEqual(res.postings, [])
        self.assertEqual(res.declared, 0)
        self.assertEqual(res.empty_evidence, "Currently there are no job postings available")

    def test_cards_that_stop_matching_are_zero_rows_with_no_evidence(self):
        """The dangerous case: the page still has jobs, the selector no longer sees them."""
        html = page("skima_with_jobs.html").replace('class="text-lg', 'class="text-xl')
        res = skima("mccafe.skima.ai", {"https://mccafe.skima.ai/": html}).fetch()
        self.assertEqual(res.postings, [])
        self.assertEqual(res.declared, 5)                        # the page still says 5
        self.assertIsNone(res.empty_evidence)                    # and nothing claims it is empty

    def test_a_page_without_the_job_search_is_a_layout_change(self):
        with self.assertRaisesRegex(AdapterError, "layout changed"):
            skima("careers.nykaa.com", {"https://careers.nykaa.com/": "<html>maintenance</html>"}).fetch()

    def test_a_later_page_is_only_trusted_if_it_says_it_is_that_page(self):
        first = page("skima_with_jobs.html").replace('data-last-page="1"', 'data-last-page="2"')
        routes = {"https://mccafe.skima.ai/": first,
                  "https://mccafe.skima.ai/?page=2": first}      # the site ignores ?page and repeats page 1
        a = skima("mccafe.skima.ai", routes)
        res = a.fetch()
        self.assertEqual(len(res.postings), 5)                   # not 10, and no duplicates
        self.assertIn(("GET", "https://mccafe.skima.ai/?page=2", None), a.http.calls)


if __name__ == "__main__":
    unittest.main()
