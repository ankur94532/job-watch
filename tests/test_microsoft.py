import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fakes import FakeHttp
from tests.test_eightfold_anchored import Board
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
    def test_expiring_paging_work_preserves_api_cooldown_and_escalation(self):
        adapter = build_adapter("microsoft", dict(CFG, anchor_resume=True),
                                http=FakeHttp(), page_delay=0)
        old = adapter._fresh_cursor()
        old.update(start=20, ids=["1000"], api_after=NOW + 4 * 3600, api_backoff_level=3)
        fresh = adapter.expired_cursor(old)
        self.assertEqual(fresh["start"], 0)
        self.assertEqual(fresh["ids"], [])
        self.assertEqual(fresh["api_after"], NOW + 4 * 3600)
        self.assertEqual(fresh["api_backoff_level"], 3)
        self.assertEqual(fresh["scope"], adapter.cursor_scope())

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

    def test_count_drift_restart_still_waits_before_the_next_api_call(self):
        http = FakeHttp({lambda m, u, p: "/api/pcsx/search" in u: api(88)})
        adapter = build_adapter("microsoft", CFG, http=http, page_delay=0)
        ids = [str(1000 + i) for i in range(10)]
        result = adapter.fetch(FetchContext(cursor={
            "loc": 0, "start": 10, "ids": ids, "loc_ids": ids, "total": 89,
            "scope": adapter.cursor_scope(), "api_after": NOW - 1,
        }, last_complete=NOW - 2 * 86400, now=NOW))
        self.assertTrue(result.restart)
        self.assertEqual(result.cursor["start"], 0)
        self.assertEqual(result.cursor["ids"], [])
        self.assertEqual(result.cursor["scope"], adapter.cursor_scope())
        self.assertEqual(result.cursor["api_after"], NOW + 3600)

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


class MicrosoftAllRoles(unittest.TestCase):
    """Unfiltered closure sweep at two requests per hourly slice, list moving."""

    CFG = {"ats": "microsoft", "host": "apply.careers.microsoft.com",
           "domain": "microsoft.com", "locations": ["India"], "page_delay": 0,
           "anchor_resume": True, "max_pages_per_run": 2, "resync_budget": 2,
           "resync_slices": 12, "closure_resume_interval": 3600}

    def test_sweeps_every_role_while_jobs_are_reposted_added_and_closed(self):
        board = Board(229)
        existing = board.ids()
        adapter = build_adapter("microsoft", self.CFG, http=board.http, page_delay=0)
        rng = __import__("random").Random(3)
        cursor, seen, now = None, set(), NOW
        for hour in range(60):
            before = board.http.requests
            result = adapter.fetch(FetchContext(cursor=cursor, now=now,
                                                last_complete=NOW - 2 * 86400))
            self.assertLessEqual(board.http.requests - before, 2)
            self.assertFalse(result.restart, result)
            seen |= {p.job_id for p in result.postings}
            if result.complete:
                break
            cursor = result.cursor
            self.assertIn("scope", cursor)
            now += 3600
            # A heavier hour than Microsoft's real one (its newest ten roles
            # spanned 18 hours on 2026-09-22): a re-post jumps to the front, a
            # new role appears, and a role that was already read closes.
            job = board.jobs[rng.randrange(len(board.jobs))]
            board.remove(job[0])
            board.add(job[0], now)
            board.add(900000 + hour, now + 1)
            read = [j for j in board.jobs if str(j[0]) in seen]
            if read:
                board.remove(read[rng.randrange(len(read))][0])
        self.assertTrue(result.complete, "sweep did not finish in 60 hourly slices")
        # Every role that existed when the sweep began and is still open was
        # read, re-posted ones included. A role first posted after the second
        # pass passed the top cannot be falsely closed: it was never stored.
        self.assertTrue(existing & board.ids() <= seen)
        for _, url, _ in board.http.calls:
            self.assertNotIn("filter_profession", url)

    def test_anchored_cursor_keeps_its_scope_so_resumes_do_not_restart(self):
        board = Board(40)
        adapter = build_adapter("microsoft", self.CFG, http=board.http, page_delay=0)
        first = adapter.fetch(FetchContext(now=NOW, last_complete=NOW - 2 * 86400))
        self.assertEqual(first.cursor["scope"], adapter.cursor_scope())
        second = adapter.fetch(FetchContext(cursor=first.cursor, now=NOW + 3600,
                                            last_complete=NOW - 2 * 86400))
        self.assertFalse(second.restart)
        self.assertEqual(second.cursor["start"], 40 if second.complete else second.cursor["start"])
        self.assertGreater(len(second.postings), 0)

    def test_posting_faster_than_the_quota_restarts_instead_of_guessing(self):
        board = Board(60)
        adapter = build_adapter("microsoft", self.CFG, http=board.http, page_delay=0)
        cursor, now = None, NOW
        for hour in range(40):
            result = adapter.fetch(FetchContext(cursor=cursor, now=now,
                                                last_complete=NOW - 2 * 86400))
            if result.complete or result.restart:
                break
            cursor, now = result.cursor, now + 3600
            for n in range(25):                    # far more new rows per hour than two pages
                board.add(800000 + hour * 100 + n, now + n)
        self.assertTrue(result.restart)
        self.assertFalse(result.complete)
