import os
import tempfile
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
            "defaults": {"title": ["software engineer", "backend"], "location": ["india", "bengaluru", "remote"]},
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


if __name__ == "__main__":
    unittest.main()
