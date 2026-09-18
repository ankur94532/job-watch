import os
import tempfile
import threading
import time
import unittest

from tests.fakes import Clock, RecordingSink
from watcher import runner
from watcher.adapters import ADAPTERS, Adapter, FetchResult, Posting
from watcher.http import HttpStatusError, NetworkError, NotModified
from watcher.store import PENDING, SENT, SUPPRESSED, Store


class FakeBoard(Adapter):
    """In-memory adapter; tests mutate BOARDS[token] between runs."""

    ats = "fake"
    required = ("token",)
    BOARDS = {}
    CALLS = []

    def fetch(self, ctx=None):
        FakeBoard.CALLS.append(self.company)
        board = FakeBoard.BOARDS[self.cfg["token"]]
        if isinstance(board, Exception):
            raise board
        return FetchResult([
            Posting(self.company, "fake", jid, title, loc, False, posted, f"https://x/{jid}")
            for jid, title, loc, posted in board
        ])


class FakePaged(FakeBoard):
    ats = "fakeworkday"
    paginated = True


def job(jid, title="Software Engineer", loc="Bengaluru, India", posted="2026-09-15"):
    return (jid, title, loc, posted)


class Base(unittest.TestCase):
    def setUp(self):
        ADAPTERS["fake"] = FakeBoard
        ADAPTERS["fakeworkday"] = FakePaged
        FakeBoard.BOARDS = {}
        FakeBoard.CALLS = []
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "jobs.db"))
        self.clock = Clock()
        self.sink = RecordingSink()
        self.online = True
        self.cfg = {
            # Serial by default: the ordering and budget tests share one fake clock
            # and assert on call order. The Parallel class opts back in.
            "defaults": {"title": ["software engineer", "backend"], "location": ["india", "bengaluru", "remote"],
                         "parallel": 1},
            "companies": {},
        }

    def tearDown(self):
        ADAPTERS.pop("fake", None)
        ADAPTERS.pop("fakeworkday", None)
        self.store.close()
        self.tmp.cleanup()

    def company(self, name, board, **cfg):
        FakeBoard.BOARDS[name] = board
        self.cfg["companies"][name] = dict({"ats": "fake", "token": name}, **cfg)

    def run_once(self, seed=False, advance=900):
        FakeBoard.CALLS = []
        res = runner.run(self.cfg, self.store, sink=self.sink, seed=seed, http=object(),
                         page_delay=0, company_delay=0, clock=self.clock, monotonic=self.clock.monotonic,
                         sleep=self.clock.sleep, online=lambda: self.online)
        self.clock.advance(advance)
        return res


class SeedAndDiff(Base):
    def test_seed_is_silent_then_only_new_ids_alert(self):
        self.company("acme", [job("1"), job("2")])
        res = self.run_once(seed=True)
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(res.ok[0][1], 2)

        self.assertEqual(self.run_once().sent, 0)
        FakeBoard.BOARDS["acme"] = [job("1"), job("2"), job("3", "Backend Engineer")]
        res = self.run_once()
        self.assertEqual(res.sent, 1)
        self.assertEqual(len(self.sink.messages), 1)
        self.assertIn("Backend Engineer", self.sink.messages[0])

        self.sink.messages.clear()
        self.run_once()
        self.assertEqual(self.sink.messages, [])          # silence when nothing is new

    def test_eleven_hour_gap_reports_everything_between_regardless_of_posted_date(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        self.clock.advance(11 * 3600)
        FakeBoard.BOARDS["acme"] = [job("1"), job("2", posted="Posted 30+ Days Ago"),
                                    job("3", posted="2025-01-01"), job("4")]
        res = self.run_once()
        self.assertEqual(res.sent, 3)
        self.assertEqual(self.run_once(advance=900).sent, 0)

    def test_unseeded_company_records_silently_on_first_run(self):
        self.company("acme", [job(str(i)) for i in range(40)])
        res = self.run_once()
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(res.sent, 0)
        states = {r["notified"] for r in self.store.rows(company="acme")}
        self.assertEqual(states, {SUPPRESSED})

    def test_non_matches_never_alert(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = [job("1"), job("2", "Account Executive"), job("3", loc="Austin, TX")]
        self.assertEqual(self.run_once().sent, 0)

    def test_failed_delivery_stays_pending_and_retries(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = [job("1"), job("2")]
        self.sink.ok = False
        res = self.run_once()
        self.assertEqual(res.sent, 0)
        self.assertEqual([r["notified"] for r in self.store.rows() if r["job_id"] == "2"], [PENDING])
        self.sink.ok = True
        self.assertEqual(self.run_once().sent, 1)

    def test_digest_marks_everything_notified(self):
        self.company("acme", [job("0")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = [job(str(i)) for i in range(60)]
        res = self.run_once()
        self.assertEqual(len(self.sink.messages), 1)
        self.assertIn("59 new matches", self.sink.messages[0])
        self.assertEqual(res.sent, 59)
        self.assertEqual({r["notified"] for r in self.store.rows() if r["job_id"] != "0"}, {SENT})
        self.sink.messages.clear()
        self.run_once()
        self.assertEqual(self.sink.messages, [])

    def test_closed_reqs(self):
        self.company("acme", [job("1"), job("2")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = [job("1")]
        self.run_once()
        self.assertEqual([r["job_id"] for r in self.store.rows(closed=True)], ["2"])


class Scheduling(Base):
    def test_not_due_is_reported_separately(self):
        self.company("acme", [job("1")])
        self.company("big", [job("9")], ats="fakeworkday")
        self.cfg["companies"]["big"]["interval"] = 3600
        self.run_once(seed=True, advance=0)
        res = self.run_once(advance=900)
        self.assertEqual(sorted(n for n, _ in res.not_due), ["acme", "big"])
        res = self.run_once(advance=900)                 # t=900
        self.assertEqual([n for n, *_ in res.ok], ["acme"])
        self.assertEqual([n for n, _ in res.not_due], ["big"])
        self.clock.advance(1800)                          # t=3600
        res = self.run_once()
        self.assertEqual(sorted(n for n, *_ in res.ok), ["acme", "big"])

    def test_paginated_boards_default_to_hourly(self):
        from watcher.config import interval_for
        for ats in ("workday", "eightfold", "amazon"):          # many requests per sweep
            self.assertEqual(interval_for({"ats": ats}, {}), 3600, ats)
        for ats in ("greenhouse", "lever", "ashby", "atlassian"):  # one request, ETag
            self.assertEqual(interval_for({"ats": ats}, {}), 900, ats)
        self.assertEqual(interval_for({"ats": "workday", "interval": 900}, {}), 900)

    def test_single_request_boards_go_first(self):
        self.company("big", [job("9")], ats="fakeworkday")
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        self.assertEqual(FakeBoard.CALLS, ["acme", "big"])

    def test_no_company_starves_when_the_budget_only_fits_a_few(self):
        """The bug: sorting on `paginated` first partitioned the plan, and with
        the cheap boards alone overflowing the budget the paginated ones were
        NEVER reached. Seventeen real companies went unpolled for two days and
        their exports kept listing requisitions that had closed."""
        for i in range(6):
            self.company("cheap%d" % i, [job("c%d" % i)])
        for i in range(3):
            self.company("paged%d" % i, [job("p%d" % i)], ats="fakeworkday")
        everyone = set(self.cfg["companies"])

        # Each fetch burns 20s of a 50s budget: about two companies per run.
        self.cfg["defaults"].update({"run_budget": 50, "company_delay": 0, "page_delay": 0})
        orig = FakeBoard.fetch

        def slow(self_, ctx=None):
            self.clock.mono += 20
            return orig(self_, ctx)
        FakeBoard.fetch = slow
        try:
            polled = set()
            for _ in range(12):                 # 12 runs x ~2 = ample for 9 companies
                self.run_once(advance=3600)     # past every interval, so all are due
                polled.update(FakeBoard.CALLS)
        finally:
            FakeBoard.fetch = orig

        self.assertEqual(polled, everyone,
                         "never polled: %s" % sorted(everyone - polled))

    def test_the_longest_waiting_company_is_polled_first(self):
        self.company("fresh", [job("1")])
        self.company("stale", [job("2")])
        self.store.sweep_complete("fresh", self.clock(), None, 1, {})
        self.store.sweep_complete("stale", self.clock() - 86400, None, 1, {})
        self.run_once(advance=3600)
        self.assertEqual(FakeBoard.CALLS[0], "stale")

    def test_a_paginated_board_still_goes_first_when_it_is_the_stalest(self):
        """Cost is a tie-break, not a partition -- that partition was the bug."""
        self.company("cheap", [job("1")])
        self.company("heavy", [job("2")], ats="fakeworkday")
        self.store.sweep_complete("cheap", self.clock(), None, 1, {})
        self.store.sweep_complete("heavy", self.clock() - 86400, None, 1, {})
        self.run_once(advance=3600)
        self.assertEqual(FakeBoard.CALLS[0], "heavy")

    def starve(self, name, polled_ago, complete_ago):
        """Leave `name` far behind AND make sure this run cannot reach it.

        The check runs after polling, on purpose: a company that just got its
        turn is no longer starving. So the warning is only meaningful for one
        that stays unreached, which is what the real bug looked like -- deferred
        every single run while the summary said everything was fine.
        """
        self.company("blocker", [job("0")])
        self.company(name, [job("1")])
        self.run_once(advance=0)
        now = self.clock()
        # last_complete is the sweep's START, so it must be passed explicitly.
        self.store.sweep_complete(name, now - complete_ago, now - complete_ago, 1, {})
        self.store._set(name, last_polled=now - polled_ago)
        self.store.sweep_complete("blocker", now - 100000, None, 1, {})
        self.cfg["defaults"].update({"run_budget": 50, "overrun": 0})   # a hard stop at 50s
        orig = FakeBoard.fetch

        def slow(self_, ctx=None):          # blocker eats the whole budget
            self.clock.mono += 45
            return orig(self_, ctx)
        FakeBoard.fetch = slow
        try:
            return self.run_once(advance=0)
        finally:
            FakeBoard.fetch = orig

    def test_a_starved_company_is_reported_loudly(self):
        """The starvation bug was invisible: every run said `ok` while a third of
        the boards went unpolled for two days. Falling behind must announce
        itself, not wait for someone to notice a dead link."""
        with self.assertLogs(runner.log, "WARNING") as logs:
            res = self.starve("a", polled_ago=20 * 900, complete_ago=20 * 900)
        self.assertIn(("a", "poll"), [(n, k) for n, _, _, k in res.starving])
        self.assertIn("has not been polled", "\n".join(logs.output))

    def test_a_company_that_never_finishes_a_sweep_is_reported(self):
        """Polled fine but never completing: closed reqs are never retired and
        the phone view keeps linking to postings that have gone. Walmart sat in
        exactly this state while looking perfectly healthy."""
        with self.assertLogs(runner.log, "WARNING") as logs:
            res = self.starve("a", polled_ago=60, complete_ago=30 * 900)
        self.assertIn(("a", "sweep"), [(n, k) for n, _, _, k in res.starving])
        self.assertIn("COMPLETED a sweep", "\n".join(logs.output))

    def test_a_backed_off_company_is_not_called_starving(self):
        """Backoff is a deliberate pause, not neglect -- warning about it would
        train you to ignore the warning."""
        self.company("a", [job("1")])
        self.run_once(advance=0)
        self.store.sweep_complete("a", self.clock() - 20 * 900, None, 1, {})
        self.store.back_off("a", "blocked", self.clock() + 3600, "HTTP 429")
        self.clock.advance(1)
        res = self.run_once(advance=0)
        self.assertEqual(res.starving, [])

    def test_a_healthy_company_is_never_called_starving(self):
        self.company("a", [job("1")])
        res = self.run_once()
        self.assertEqual(res.starving, [])

    def test_pacing_comes_from_config(self):
        self.cfg["defaults"].update({"company_delay": 0, "page_delay": 0, "run_budget": 30})
        self.company("a", [job("1")])
        slept = []
        orig_sleep = self.clock.sleep

        def spy(s):
            slept.append(s)
            orig_sleep(s)
        self.clock.sleep = spy
        self.company("b", [job("2")])
        res = self.run_once()
        self.clock.sleep = orig_sleep
        self.assertEqual(len(res.ok), 2)
        self.assertEqual([s for s in slept if s], [])      # config zeroed the delays

    def test_a_useless_run_budget_is_clamped_not_obeyed(self):
        self.cfg["defaults"]["run_budget"] = 2          # would defer every company forever
        self.company("a", [job("1")])
        with self.assertLogs(runner.log, "WARNING"):
            res = self.run_once()
        self.assertEqual(len(res.ok), 1)

    def test_out_of_time_companies_are_deferred(self):
        self.cfg["defaults"]["overrun"] = 0
        self.company("a", [job("1")])
        self.company("b", [job("2")])

        def slow(*_):
            self.clock.mono += 60
        orig = FakeBoard.fetch
        FakeBoard.fetch = lambda self_, ctx=None: (slow(), orig(self_, ctx))[1]
        try:
            res = self.run_once()
        finally:
            FakeBoard.fetch = orig
        self.assertEqual(len(res.ok), 1)
        self.assertEqual(len(res.deferred), 1)

    def slow_fetches(self, seconds):
        orig = FakeBoard.fetch

        def slow(self_, ctx=None):
            self.clock.mono += seconds
            return orig(self_, ctx)
        FakeBoard.fetch = slow
        self.addCleanup(setattr, FakeBoard, "fetch", orig)

    def test_single_request_boards_overrun_the_budget_so_none_wait_a_run(self):
        """The ~22 Greenhouse boards share one host and need ~65s: a strict 50s
        budget left a third of them for the next run, every run."""
        self.cfg["defaults"].update({"run_budget": 50, "overrun": 120})
        for i in range(6):
            self.company("gh%d" % i, [job(str(i))])
        self.slow_fetches(15)                    # 6 x 15s = 90s: past 50, inside 170
        res = self.run_once()
        self.assertEqual(len(res.ok), 6)
        self.assertEqual(res.deferred, [])

    def test_multi_page_sweeps_still_stop_at_the_budget(self):
        self.cfg["defaults"].update({"run_budget": 50, "overrun": 120})
        for i in range(4):
            self.company("wd%d" % i, [job(str(i))], ats="fakeworkday")
        self.slow_fetches(30)
        res = self.run_once()
        self.assertEqual(len(res.ok), 2)         # 0s and 30s start; at 60s the budget is gone
        self.assertEqual(len(res.deferred), 2)

    def test_the_overrun_has_a_ceiling(self):
        """A stalled host must not hold the run open until the next one is due."""
        self.cfg["defaults"].update({"run_budget": 50, "overrun": 120})
        for i in range(10):
            self.company("gh%d" % i, [job(str(i))])
        self.slow_fetches(40)                    # starts at 0,40,...,160; 200 > 170 is out
        res = self.run_once()
        self.assertEqual(len(res.ok), 5)
        self.assertEqual(len(res.deferred), 5)


class Parallel(Base):
    """Hosts are polled at the same time; one host never sees two polls at once.

    Serial polling was the capacity ceiling: 44 fifteen-minute boards alone
    needed ~88s of a 50s run, so whichever order the plan took, most of them
    starved. The per-request pacing is politeness towards a *server*; nothing
    about it requires Greenhouse to wait for Workday.
    """

    def setUp(self):
        super().setUp()
        self.cfg["defaults"]["parallel"] = 8
        self.lock = threading.Lock()
        self.active = {}          # lane -> polls in flight
        self.peak = {}            # lane -> most ever in flight at once
        self.orig = FakeBoard.fetch

    def tearDown(self):
        FakeBoard.fetch = self.orig
        FakePaged.max_parallel = None
        super().tearDown()

    def track(self, key_of, hold=0.02):
        """Wrap fetch to record how many polls overlap, per whatever key_of says."""
        orig = self.orig

        def fetch(self_, ctx=None):
            key = key_of(self_)
            with self.lock:
                self.active[key] = self.active.get(key, 0) + 1
                self.peak[key] = max(self.peak.get(key, 0), self.active[key])
            try:
                time.sleep(hold)                 # real time: long enough to overlap
                return orig(self_, ctx)
            finally:
                with self.lock:
                    self.active[key] -= 1
        FakeBoard.fetch = fetch

    def test_different_hosts_really_are_polled_at_the_same_time(self):
        # Each fetch waits for the other: serial polling would break the barrier.
        barrier = threading.Barrier(2, timeout=5)
        orig = self.orig

        def fetch(self_, ctx=None):
            barrier.wait()
            return orig(self_, ctx)
        FakeBoard.fetch = fetch
        self.company("gh", [job("1")], host="greenhouse.example")
        self.company("wd", [job("2")], host="workday.example")
        res = self.run_once()
        self.assertEqual(res.failed, [])
        self.assertEqual(sorted(n for n, *_ in res.ok), ["gh", "wd"])

    def test_one_host_is_never_polled_twice_at_once(self):
        for i in range(4):
            self.company("a%d" % i, [job("a%d" % i)], host="a.example")
            self.company("b%d" % i, [job("b%d" % i)], host="b.example")
        self.track(lambda a: a.lane())
        res = self.run_once()
        self.assertEqual(len(res.ok), 8)
        self.assertEqual(self.peak, {"fake:a.example": 1, "fake:b.example": 1})

    def test_boards_without_their_own_host_share_one_lane(self):
        """All Greenhouse boards hit boards-api.greenhouse.io: they must queue."""
        for i in range(4):
            self.company("gh%d" % i, [job(str(i))])
        self.track(lambda a: a.ats)
        res = self.run_once()
        self.assertEqual(len(res.ok), 4)
        self.assertEqual(self.peak, {"fake": 1})

    def test_max_parallel_caps_one_ats_across_its_hosts(self):
        """Workday tenants are separate subdomains on shared clusters: capped."""
        FakePaged.max_parallel = 2
        for i in range(5):
            self.company("wd%d" % i, [job(str(i))], ats="fakeworkday", host="t%d.example" % i)
        self.track(lambda a: a.ats, hold=0.05)
        res = self.run_once()
        self.assertEqual(len(res.ok), 5)
        self.assertEqual(self.peak, {"fakeworkday": 2})

    def test_priority_order_still_holds_on_a_shared_host(self):
        for name, ago in (("fresh", 1000), ("stalest", 90000), ("stale", 5000)):
            self.company(name, [job(name)])
            self.store.sweep_complete(name, self.clock() - ago, self.clock() - ago, 1, {})
        self.company("elsewhere", [job("x")], host="other.example")
        self.run_once()
        self.assertEqual([c for c in FakeBoard.CALLS if c != "elsewhere"], ["stalest", "stale", "fresh"])

    def test_a_crash_in_a_worker_is_recorded_not_swallowed(self):
        self.company("ok", [job("1")], host="a.example")
        self.company("boom", [job("2")], host="b.example")
        orig = runner._poll_company

        def poll(name, *args):
            if name == "boom":
                raise RuntimeError("store exploded")
            return orig(name, *args)
        runner._poll_company = poll
        try:
            with self.assertLogs(runner.log, "ERROR"):
                res = self.run_once()
        finally:
            runner._poll_company = orig
        self.assertEqual([n for n, *_ in res.ok], ["ok"])
        self.assertEqual(res.failed, [("boom", "RuntimeError: store exploded")])

    def test_out_of_time_is_still_deferred_with_threads(self):
        self.cfg["defaults"]["overrun"] = 0
        for i in range(3):
            self.company("a%d" % i, [job("a%d" % i)], host="a.example")
            self.company("b%d" % i, [job("b%d" % i)], host="b.example")
        orig = self.orig

        def slow(self_, ctx=None):
            with self.lock:
                self.clock.mono += 30          # two overlapping polls exhaust the 50s
            return orig(self_, ctx)
        FakeBoard.fetch = slow
        res = self.run_once()
        self.assertTrue(res.deferred)
        self.assertEqual(sorted([n for n, *_ in res.ok] + res.deferred), sorted(self.cfg["companies"]))

    def test_offline_mid_run_stops_every_worker(self):
        for i in range(3):
            self.company("a%d" % i, [job("a%d" % i)], host="a.example")
            self.company("b%d" % i, [job("b%d" % i)], host="b.example")
        orig = self.orig

        def drop(self_, ctx=None):
            self.online = False
            raise NetworkError("no route to host")
        FakeBoard.fetch = drop
        res = self.run_once()
        self.assertTrue(res.offline)
        self.assertLessEqual(len(FakeBoard.CALLS), 2)   # at most the two already in flight
        self.assertEqual(res.deferred, [])


class FilterSafety(Base):
    """A filter that matches everything must never be silent."""

    def test_open_filter_stops_the_company_before_it_polls(self):
        from watcher.filters import FilterTooLoose, Filter, sanity_check
        self.cfg["defaults"] = {"title": [], "location": []}
        self.company("acme", [job("1")])
        with self.assertRaises(FilterTooLoose):
            sanity_check(Filter(title=[], location=[]), "acme")
        res = self.run_once()
        self.assertEqual(res.ok, [])
        self.assertEqual(len(res.failed), 1)
        self.assertIn("matching everything", res.failed[0][1])
        self.assertEqual(FakeBoard.CALLS, [])          # never even fetched

    def test_a_string_config_value_no_longer_matches_everything(self):
        from watcher.filters import Filter, sanity_check
        f = Filter(title="software engineer", location="india")
        sanity_check(f, "string-config")               # would raise before the fix
        self.assertFalse(f.location_ok("Reykjavik, Iceland"))

    def test_every_row_matching_is_reported(self):
        self.cfg["defaults"] = {"title": ["software engineer"], "location": ["india"]}
        self.company("acme", [job(str(i)) for i in range(6)])
        with self.assertLogs(runner.log, "WARNING") as logs:
            self.run_once(seed=True)
        self.assertIn("ALL 6 rows match", "".join(logs.output))

    def test_window_limited_board_warns_when_the_whole_window_is_new(self):
        class Windowed(FakeBoard):
            ats = "fake"
            window_limited = True
        ADAPTERS["fake"] = Windowed
        self.company("goog", [job("1")])
        self.run_once(seed=True)                        # seeded, so later rows are genuinely new
        FakeBoard.BOARDS["goog"] = [job(str(i)) for i in range(20, 26)]
        with self.assertLogs(runner.log, "WARNING") as logs:
            self.run_once()
        self.assertIn("likely overflowed", "".join(logs.output))


class Backoff(Base):
    def test_429_backs_off_exponentially_persisted_and_resets(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = HttpStatusError("u", 429)

        res = self.run_once(advance=0)
        st = self.store.state("acme")
        self.assertEqual(st["blocked_until"] - int(self.clock.t), 1800)
        self.assertEqual(len(res.failed), 1)

        for _ in range(3):                                # launchd keeps firing; we don't touch it
            self.clock.advance(500)
            res = self.run_once(advance=0)
            self.assertEqual(FakeBoard.CALLS, [])
            self.assertEqual([n for n, *_ in res.backed_off], ["acme"])

        self.clock.advance(300)
        self.run_once(advance=0)
        self.assertEqual(self.store.state("acme")["blocked_until"] - int(self.clock.t), 3600)

        FakeBoard.BOARDS["acme"] = [job("1")]
        self.clock.advance(3600)
        res = self.run_once()
        self.assertEqual([n for n, *_ in res.ok], ["acme"])
        st = self.store.state("acme")
        self.assertIsNone(st["blocked_until"])
        self.assertEqual(st["backoff_level"], 0)

    def test_blocked_ceiling_is_a_day_and_server_ceiling_an_hour(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = HttpStatusError("u", 403)
        for _ in range(10):
            self.clock.t = self.store.state("acme")["blocked_until"] or self.clock.t
            self.run_once(advance=0)
        st = self.store.state("acme")
        self.assertEqual(st["blocked_until"] - int(self.clock.t), 24 * 3600)

        self.company("srv", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["srv"] = HttpStatusError("u", 503)
        for _ in range(5):
            self.clock.t = max(self.store.state("srv")["blocked_until"] or 0, self.clock.t)
            self.run_once(advance=0)
        st = self.store.state("srv")
        self.assertEqual(st["blocked_until"] - int(self.clock.t), 3600)

    def test_retry_after_is_respected(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = HttpStatusError("u", 429, retry_after=5 * 3600)
        self.run_once(advance=0)
        self.assertEqual(self.store.state("acme")["blocked_until"] - int(self.clock.t), 5 * 3600)

    def test_extended_backoff_notifies_once(self):
        self.company("acme", [job("1")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = HttpStatusError("u", 429)
        for _ in range(6):
            self.clock.t = max(self.store.state("acme")["blocked_until"] or 0, self.clock.t)
            self.run_once(advance=0)
        warnings = [m for m in self.sink.messages if "extended backoff" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn("acme", warnings[0])


class ThrottledMidSweep(Base):
    """A board that 429s partway through must not throw away the rows already paid for."""

    def setUp(self):
        super().setUp()
        self.pages = [[job("1"), job("2")], [job("3")]]

        class Throttling(FakePaged):
            calls = []

            def fetch(self_, ctx=None):
                FakeBoard.CALLS.append(self_.company)
                rows = self.pages[0] if ctx.cursor is None else self.pages[1]
                postings = [Posting(self_.company, "fake", *r[:3], False, r[3], f"https://x/{r[0]}")
                            for r in rows]
                if ctx.cursor is None:
                    return self_.partial_on_throttle(
                        HttpStatusError("u", 429), postings, {"offset": 2})
                return FetchResult(postings)

        ADAPTERS["fakeworkday"] = Throttling

    def test_partial_rows_are_kept_and_the_company_is_parked(self):
        self.company("big", None, ats="fakeworkday")
        res = self.run_once(seed=True, advance=0)
        self.assertEqual(len(res.partial), 1)
        self.assertEqual([n for n, *_ in res.backed_off], ["big"])          # parked...
        self.assertEqual(len(self.store.rows(company="big")), 2)            # ...but rows kept
        st = self.store.state("big")
        self.assertEqual(st["cursor"], {"offset": 2})                       # ...and progress kept
        self.assertTrue(st["blocked_until"] > int(self.clock.t))

        self.clock.advance(1900)                                            # backoff expires
        res = self.run_once()
        self.assertEqual([n for n, *_ in res.ok], ["big"])
        self.assertEqual(len(self.store.rows(company="big")), 3)            # sweep finished
        self.assertIsNone(self.store.state("big")["cursor"])

    def test_progress_does_not_escalate_the_backoff(self):
        """A board that throttles every sweep but keeps advancing must stay at the base wait."""

        class AlwaysThrottles(FakePaged):
            n = 0

            def fetch(self_, ctx=None):
                FakeBoard.CALLS.append(self_.company)
                AlwaysThrottles.n += 1
                p = Posting(self_.company, "fake", str(AlwaysThrottles.n), "Software Engineer",
                            "Bengaluru, India", False, "2026-09-15", "https://x")
                return self_.partial_on_throttle(
                    HttpStatusError("u", 429), [p], {"offset": AlwaysThrottles.n})

        ADAPTERS["fakeworkday"] = AlwaysThrottles
        self.company("big", None, ats="fakeworkday")
        for _ in range(4):
            self.run_once(seed=True, advance=0)
            st = self.store.state("big")
            self.assertEqual(st["blocked_until"] - int(self.clock.t), 1800)   # never 1h, 2h, 24h
            self.assertEqual(st["backoff_level"], 0)
            self.clock.advance(1900)
        self.assertEqual(len(self.store.rows(company="big")), 4)              # a row kept each time

    def test_completing_a_sweep_clears_a_non_escalated_park(self):
        self.company("big", None, ats="fakeworkday")
        self.run_once(seed=True, advance=0)
        self.assertTrue(self.store.state("big")["blocked_until"])
        self.clock.advance(1900)
        self.run_once()
        st = self.store.state("big")
        self.assertIsNone(st["blocked_until"])
        self.assertIsNone(st["backoff_kind"])

    def test_a_throttle_before_any_rows_still_just_fails(self):
        class Immediate(FakePaged):
            def fetch(self_, ctx=None):
                FakeBoard.CALLS.append(self_.company)
                raise HttpStatusError("u", 429)

        ADAPTERS["fakeworkday"] = Immediate
        self.company("big", None, ats="fakeworkday")
        res = self.run_once(seed=True, advance=0)
        self.assertEqual(res.partial, [])
        self.assertEqual(len(res.failed), 1)
        self.assertEqual(self.store.rows(company="big"), [])


class Laptop(Base):
    def test_offline_is_quiet_and_touches_nothing(self):
        self.company("acme", [job("1")])
        self.online = False
        res = self.run_once()
        self.assertTrue(res.offline)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(FakeBoard.CALLS, [])
        self.assertIsNone(self.store.state("acme")["last_polled"])
        self.assertEqual(self.sink.messages, [])

    def test_network_drop_mid_run_stops_quietly(self):
        self.company("a", NetworkError("dns"))
        self.company("b", [job("2")])
        self.online = True
        orig = FakeBoard.fetch

        def fetch(self_, ctx=None):
            self.online = False
            return orig(self_, ctx)
        FakeBoard.fetch = fetch
        try:
            res = self.run_once()
        finally:
            FakeBoard.fetch = orig
        self.assertTrue(res.offline)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.failed, [])
        self.assertEqual(self.sink.messages, [])

    def test_one_failure_does_not_fail_the_run(self):
        self.company("bad", ValueError("garbage"))
        self.company("good", [job("1")])
        res = self.run_once()
        self.assertEqual(res.exit_code, 0)
        self.assertEqual([n for n, _ in res.failed], ["bad"])

    def test_all_failing_exits_nonzero(self):
        self.company("bad", ValueError("garbage"))
        self.assertEqual(self.run_once().exit_code, 1)

    def test_not_modified_keeps_rows_open(self):
        self.company("acme", [job("1"), job("2")])
        self.run_once(seed=True)
        FakeBoard.BOARDS["acme"] = NotModified("u")
        res = self.run_once()
        self.assertEqual(res.unchanged, ["acme"])
        self.assertEqual(self.store.rows(closed=True), [])


class PartialSweeps(Base):
    def test_partial_sweep_alerts_early_and_closes_only_after_completion(self):
        pages = {"n": 0}
        boards = [
            [job("1"), job("2"), job("3")],
        ]

        class Paged(FakePaged):
            def fetch(self_, ctx=None):
                FakeBoard.CALLS.append(self_.company)
                rows = boards[0]
                if ctx.cursor is None:
                    return FetchResult([Posting("p", "fake", *r[:3], False, r[3], "u") for r in rows[:2]],
                                       complete=False, cursor={"offset": 2, "ids": [r[0] for r in rows[:2]]})
                return FetchResult([Posting("p", "fake", *r[:3], False, r[3], "u") for r in rows[2:]])

        ADAPTERS["fakeworkday"] = Paged
        self.company("p", None, ats="fakeworkday")
        self.run_once(seed=True)                         # seed ignores the deadline: Paged still splits
        self.assertIsNone(self.store.state("p")["last_complete"])
        res = self.run_once()                            # resumes, completes silently (never complete before)
        self.assertEqual([n for n, *_ in res.ok], ["p"])
        self.assertIsNotNone(self.store.state("p")["last_complete"])

        boards[0] = [job("1"), job("4"), job("3")]       # 2 closed, 4 opened
        self.clock.advance(3600)
        res = self.run_once()
        self.assertEqual(len(res.partial), 1)
        self.assertEqual(res.sent, 1)                    # 4 alerted before the sweep finished
        self.assertEqual(self.store.rows(closed=True), [])
        res = self.run_once()
        self.assertEqual([r["job_id"] for r in self.store.rows(closed=True)], ["2"])



class FakeHtml(FakeBoard):
    """An HTML adapter: rows, plus what the page itself says (declared count,
    empty-state text). META[company] = (declared, empty_evidence)."""
    ats = "fakehtml"
    html = True
    META = {}

    def fetch(self, ctx=None):
        r = FakeBoard.fetch(self, ctx)
        r.declared, r.empty_evidence = FakeHtml.META.get(self.company, (None, None))
        return r


class HtmlAdapters(Base):
    """A web page whose markup changes does not crash: a selector quietly
    matches nothing and every poll says "no new postings", which reads exactly
    like a quiet week. Zero rows from an HTML adapter is therefore never a
    normal result, and "genuinely empty" must be told apart from "broken"."""

    def setUp(self):
        super().setUp()
        ADAPTERS["fakehtml"] = FakeHtml
        FakeHtml.META = {}

    def tearDown(self):
        ADAPTERS.pop("fakehtml", None)
        super().tearDown()

    def page(self, name, rows, declared=None, empty=None):
        FakeBoard.BOARDS[name] = rows
        FakeHtml.META[name] = (declared, empty)
        self.cfg["companies"].setdefault(name, {"ats": "fakehtml", "token": name})

    def poll(self):
        with self.assertLogs(runner.log, "INFO") as logs:
            res = self.run_once()
        return res, "\n".join(logs.output)

    def test_every_run_logs_the_row_count(self):
        self.page("acme", [job("1"), job("2")], declared=2)
        _, out = self.poll()
        self.assertIn("acme: HTML adapter parsed 2 rows (page declares 2)", out)

    def test_zero_rows_the_page_confirms_is_a_warning_but_a_valid_sweep(self):
        self.page("nykaa", [], declared=0, empty="Currently there are no job postings available")
        res, out = self.poll()
        self.assertIn("WARNING:watcher.runner:nykaa: 0 rows parsed; page confirms no openings", out)
        self.assertIn("Currently there are no job postings available", out)
        self.assertEqual(res.failed, [])
        self.assertIsNotNone(self.store.state("nykaa")["last_complete"])

    def test_zero_rows_with_no_empty_text_is_a_broken_selector_and_retires_nothing(self):
        self.page("acme", [job("1"), job("2")], declared=2)
        self.run_once(seed=True)
        self.page("acme", [], declared=None, empty=None)        # the markup changed under us
        res, out = self.poll()
        self.assertIn("NO empty-state text", out)
        self.assertIn("selector has probably stopped matching", out)
        self.assertEqual([n for n, _ in res.failed], ["acme"])
        self.assertEqual(self.store.rows(closed=True), [])       # nothing wrongly marked closed

    def test_three_zero_runs_in_a_row_are_announced_loudly_and_say_which_kind(self):
        self.page("broken", [])
        self.page("empty", [], declared=0, empty="no job postings")
        outs = [self.poll()[1] for _ in range(3)]
        self.assertNotIn("verify manually", outs[1])
        self.assertIn("HTML adapter broken returned 0 rows for 3 runs — verify manually "
                      "(NO empty-state text", outs[2])
        self.assertIn("HTML adapter empty returned 0 rows for 3 runs — verify manually "
                      "(page confirms no openings", outs[2])
        self.assertEqual(self.store.state("broken")["zero_runs"], 3)

    def test_rows_coming_back_end_the_streak(self):
        self.page("acme", [])
        self.poll(); self.poll()
        self.page("acme", [job("1")], declared=1)
        self.poll()
        self.assertEqual(self.store.state("acme")["zero_runs"], 0)

    def test_fewer_rows_than_the_page_declares_is_a_partial_parse(self):
        self.page("medianet", [job("1"), job("2")], declared=2)
        self.run_once(seed=True)
        self.page("medianet", [job("1")], declared=7)            # 7 on the page, 1 matched
        res, out = self.poll()
        self.assertIn("parsed 1 rows but the page declares 7", out)
        self.assertEqual([n for n, _ in res.failed], ["medianet"])
        self.assertEqual(self.store.rows(closed=True), [])       # "2" is not retired on a partial parse

    def test_a_sharp_drop_is_called_out_even_when_the_page_agrees(self):
        self.page("acme", [job(str(i)) for i in range(40)], declared=40)
        self.run_once(seed=True)
        self.page("acme", [job(str(i)) for i in range(3)], declared=3)
        _, out = self.poll()
        self.assertIn("row count fell from 40 to 3", out)

    def test_json_adapters_are_untouched(self):
        self.company("gh", [])                                  # an API board with no openings
        with self.assertLogs(runner.log, "INFO") as logs:
            res = self.run_once()
        self.assertNotIn("HTML adapter", "\n".join(logs.output))
        self.assertEqual(res.failed, [])


class IncrementalPolls(Base):
    def test_an_incremental_poll_alerts_on_new_but_never_retires_or_counts_as_a_sweep(self):
        self.company("wf", [job("1"), job("2"), job("3")])
        self.run_once(seed=True)
        swept = self.store.state("wf")["last_complete"]
        orig = FakeBoard.fetch

        def newest_only(self_, ctx=None):
            r = orig(self_, ctx)
            r.complete, r.incremental = False, True
            return r
        FakeBoard.fetch = newest_only
        FakeBoard.BOARDS["wf"] = [job("4", "Backend Engineer"), job("1")]   # 2 and 3 simply not read
        try:
            res = self.run_once()
        finally:
            FakeBoard.fetch = orig
        self.assertEqual(res.sent, 1)                                    # 4 alerted
        self.assertEqual(self.store.rows(closed=True), [])               # 2, 3 NOT retired
        self.assertEqual(self.store.state("wf")["last_complete"], swept) # not a sweep
        self.assertGreater(self.store.state("wf")["last_polled"], swept) # but it was polled

    def test_an_incremental_board_is_not_called_sweep_starved_between_full_sweeps(self):
        from watcher.adapters.workday import FULL_SWEEP
        ccfg = {"ats": "workday", "interval": 900, "incremental": True}
        self.assertGreater(runner.sweep_threshold(ccfg, {}), FULL_SWEEP)
        self.assertEqual(runner.sweep_threshold({"ats": "fake", "interval": 900}, {}), 900 * runner.STARVING_SWEEPS)


if __name__ == "__main__":
    unittest.main()
