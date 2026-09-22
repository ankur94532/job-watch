import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.adapters.microsoft import MicrosoftAdapter
from watcher.http import HttpStatusError


CFG = {
    "ats": "microsoft", "host": "apply.careers.microsoft.com",
    "domain": "microsoft.com", "locations": ["India"],
    "profession": "software engineering", "page_delay": 0,
}
NOW = 1_800_000_000


def latest(cards=3):
    titles = "".join(
        f'<h3 class="careers-joblistResponsive-subheading">Software Engineer {i}</h3>'
        for i in range(cards))
    dates = "".join(
        '<div class="careers-joblistResponsive-postdate">2026-09-21</div>'
        for _ in range(cards))
    locations = "".join(
        '<div class="careers-joblistResponsive-primarylocation">India, Telangana, Hyderabad</div>'
        for _ in range(cards))
    worksites = "".join(
        '<div class="careers-joblistResponsive-worksiteflex">3 days / week in-office</div>'
        for _ in range(cards))
    links = "".join(
        f'<a href="https://apply.careers.microsoft.com/careers/job/{100 + i}?hl=en" '
        'class="careers-joblistResponsive-button">See details</a>'
        for i in range(cards))
    return titles + dates + locations + worksites + links + '<div id="openings" hidden>89</div>'


def api(n):
    def response(method, url, payload, cache):
        query = parse_qs(urlsplit(url).query)
        start = int(query["start"][0])
        rows = [{"id": 1000 + i, "name": f"Software Engineer {i}",
                 "locations": ["Bengaluru, India"], "postedTs": NOW - i}
                for i in range(start, min(start + 10, n))]
        return {"data": {"count": n, "sortBy": "timestamp", "positions": rows}}
    return response


class MicrosoftSafety(unittest.TestCase):
    @patch.object(MicrosoftAdapter, "check_robots")
    def test_recent_poll_reads_the_three_official_latest_cards(self, robots):
        http = FakeHttp({MicrosoftAdapter.LATEST: latest()})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        result = adapter.fetch(FetchContext(
            known={"100", "101", "102"}, last_complete=NOW - 60, now=NOW))
        self.assertTrue(result.incremental)
        self.assertFalse(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["100", "101", "102"])
        self.assertEqual(http.requests, 1)
        robots.assert_called_once_with(MicrosoftAdapter.LATEST)

    @patch.object(MicrosoftAdapter, "check_robots")
    def test_latest_layout_must_have_three_aligned_cards_and_a_count(self, robots):
        adapter = build_adapter(
            "microsoft", CFG, http=FakeHttp({MicrosoftAdapter.LATEST: latest(2)}), page_delay=0)
        with self.assertRaisesRegex(AdapterError, "layout changed"):
            adapter.fetch(FetchContext(known={"100"}, last_complete=NOW - 60, now=NOW))

    def test_complete_sweep_uses_the_official_profession_filter(self):
        http = FakeHttp({lambda m, u, p: "/api/pcsx/search" in u: api(11)})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        result = adapter.fetch(FetchContext())
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 11)
        for _, url, _ in http.calls:
            self.assertEqual(parse_qs(urlsplit(url).query)["filter_profession"],
                             ["software engineering"])

    def test_old_unfiltered_cursor_restarts_at_a_scoped_zero_cursor(self):
        http = FakeHttp()
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        result = adapter.fetch(FetchContext(cursor={
            "loc": 0, "start": 10, "ids": ["1"], "loc_ids": ["1"], "total": 235,
        }))
        self.assertTrue(result.restart)
        self.assertEqual(result.cursor["start"], 0)
        self.assertEqual(result.cursor["ids"], [])
        self.assertEqual(result.cursor["scope"], adapter.cursor_scope())
        self.assertEqual(http.calls, [])

    @patch.object(MicrosoftAdapter, "check_robots")
    def test_latest_page_keeps_polling_while_closure_api_waits(self, robots):
        http = FakeHttp({MicrosoftAdapter.LATEST: latest()})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        cursor = {
            "loc": 0, "start": 20, "ids": [str(i) for i in range(20)],
            "loc_ids": [str(i) for i in range(20)], "total": 89,
            "scope": adapter.cursor_scope(), "api_after": NOW + 3600,
        }
        result = adapter.fetch(FetchContext(
            cursor=cursor, known={"100", "101", "102"},
            last_complete=NOW - 2 * 86400, now=NOW,
        ))
        self.assertTrue(result.incremental)
        self.assertEqual(http.requests, 1)
        self.assertEqual(adapter.ats, "microsoft")
        robots.assert_called_once_with(MicrosoftAdapter.LATEST)

    def test_closure_pages_store_their_own_hourly_resume_time(self):
        cfg = dict(CFG, max_pages_per_run=2, closure_resume_interval=3600)
        http = FakeHttp({lambda m, u, p: "/api/pcsx/search" in u: api(25)})
        adapter = build_adapter("microsoft", cfg, http=http, page_delay=0)
        result = adapter.fetch(FetchContext(last_complete=NOW - 2 * 86400, now=NOW))
        self.assertFalse(result.complete)
        self.assertFalse(result.incremental)
        self.assertEqual(result.cursor["start"], 20)
        self.assertEqual(result.cursor["api_after"], NOW + 3600)
        self.assertEqual(result.cursor["api_backoff_level"], 0)
        self.assertEqual(http.requests, 2)

    @patch.object(MicrosoftAdapter, "check_robots")
    def test_api_429_delays_only_closures_not_latest_page(self, robots):
        blocked = HttpStatusError("https://apply.careers.microsoft.com/api/pcsx/search", 429)
        http = FakeHttp({
            MicrosoftAdapter.LATEST: latest(),
            lambda m, u, p: "/api/pcsx/search" in u: blocked,
        })
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        with self.assertLogs("watcher.adapters.microsoft", "WARNING"):
            partial = adapter.fetch(FetchContext(last_complete=NOW - 2 * 86400, now=NOW))
        self.assertFalse(partial.complete)
        self.assertIsNone(partial.throttled)
        self.assertEqual(partial.cursor["start"], 0)
        self.assertEqual(partial.cursor["api_after"], NOW + 3600)
        self.assertEqual(partial.cursor["api_backoff_level"], 1)

        latest_result = adapter.fetch(FetchContext(
            cursor=partial.cursor, known={"100", "101", "102"},
            last_complete=NOW - 2 * 86400, now=NOW + 900,
        ))
        self.assertTrue(latest_result.incremental)
        self.assertEqual(http.requests, 2)  # one failed API call, then one latest-page call
        robots.assert_called_once_with(MicrosoftAdapter.LATEST)

    def test_repeated_zero_progress_api_429_doubles_only_api_delay(self):
        blocked = HttpStatusError("https://apply.careers.microsoft.com/api/pcsx/search", 429)
        http = FakeHttp({lambda m, u, p: "/api/pcsx/search" in u: blocked})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        cursor = adapter._fresh_cursor()
        cursor.update(api_after=NOW, api_backoff_level=1)
        with self.assertLogs("watcher.adapters.microsoft", "WARNING"):
            result = adapter.fetch(FetchContext(cursor=cursor, now=NOW))
        self.assertEqual(result.cursor["api_after"], NOW + 7200)
        self.assertEqual(result.cursor["api_backoff_level"], 2)

    def test_api_retry_after_remains_authoritative(self):
        blocked = HttpStatusError(
            "https://apply.careers.microsoft.com/api/pcsx/search", 429,
            retry_after=5 * 3600,
        )
        http = FakeHttp({lambda m, u, p: "/api/pcsx/search" in u: blocked})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        with self.assertLogs("watcher.adapters.microsoft", "WARNING"):
            result = adapter.fetch(FetchContext(last_complete=NOW - 2 * 86400, now=NOW))
        self.assertEqual(result.cursor["api_after"], NOW + 5 * 3600)


if __name__ == "__main__":
    unittest.main()
