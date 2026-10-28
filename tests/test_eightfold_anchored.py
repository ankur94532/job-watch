"""Anchored Eightfold sweeps: a newest-first board that moves between slices."""

import random
import json
import unittest
from urllib.parse import parse_qs, urlparse

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.http import HttpStatusError

NOW = 1_800_000_000
CFG = {"ats": "eightfold", "host": "q.example", "domain": "q.com", "locations": ["India"],
       "incremental": True, "anchor_resume": True, "page_delay": 0}


class Board:
    """A live newest-first list. `jobs` is [(id, postedTs)], kept sorted."""

    def __init__(self, n):
        self.jobs = [(1000 + i, NOW - i * 60) for i in range(n)]
        self.throttle_at = set()
        self.shuffle = None          # a Random: same-timestamp rows come back in a new order per request
        self.hide = None             # a job id the server never returns (it repeats a neighbour instead)
        self.http = FakeHttp({lambda m, u, p: "/api/" in u: self.handler})

    def sort(self):
        self.jobs.sort(key=lambda j: -j[1])

    def add(self, job_id, ts):
        self.jobs.append((job_id, ts))
        self.sort()

    def remove(self, job_id):
        self.jobs = [j for j in self.jobs if j[0] != job_id]

    def handler(self, method, url, payload, cache):
        if self.http.requests in self.throttle_at:
            return HttpStatusError(url, 429)
        start = int(parse_qs(urlparse(url).query)["start"][0])
        jobs = list(self.jobs)
        if self.shuffle:
            groups = {}
            for job in jobs:
                groups.setdefault(job[1], []).append(job)
            for group in groups.values():
                self.shuffle.shuffle(group)
            jobs = [job for ts in sorted(groups, reverse=True) for job in groups[ts]]
        if self.hide is not None:
            at = [i for i, _ in jobs].index(self.hide)
            jobs[at] = jobs[at - 1] if jobs[at - 1][1] == jobs[at][1] else jobs[at + 1]
        page = jobs[start:start + 10]
        if len({i for i, _ in page}) != len(page):   # never two copies on one page
            page = [j for n, j in enumerate(page) if j not in page[:n]]
        rows = [{"id": i, "name": f"Engineer {i}", "locations": ["Bengaluru, India"],
                 "postedTs": ts} for i, ts in page]
        return {"data": {"count": len(self.jobs), "sortBy": "timestamp", "positions": rows}}

    def ids(self):
        return {str(i) for i, _ in self.jobs}


def adapter(board, **cfg):
    return build_adapter("qualcomm", dict(CFG, **cfg), http=board.http, page_delay=0)


def sweep(board, between=None, pages=3, limit=40, **cfg):
    """Run slices until complete; `between(slice_no)` mutates the board between them."""
    a = adapter(board, max_pages_per_run=pages, **cfg)
    cursor, seen = None, set()
    for n in range(limit):
        result = a.fetch(FetchContext(cursor=cursor, now=NOW))
        seen |= {p.job_id for p in result.postings}
        if result.restart:
            return result, seen
        if result.complete:
            return result, seen
        cursor = result.cursor
        if between:
            between(n)
    raise AssertionError("sweep never finished")


class AnchoredSweep(unittest.TestCase):
    def test_empty_boundary_page_inside_declared_inventory_fails_closed(self):
        board = Board(45)
        board.http = FakeHttp({lambda m, u, p: "/api/" in u:
                               {"data": {"count": 45, "positions": [], "sortBy": "timestamp"}}})
        with self.assertRaisesRegex(AdapterError, "incomplete Eightfold page"):
            adapter(board)._anchored_page("India", 10)

    def test_two_page_slices_finish_boundary_search_and_large_timestamp_group(self):
        board = Board(45)
        board.jobs = [(1000, NOW)] + [(1000 + i, NOW - 1) for i in range(1, 26)] + board.jobs[26:]
        board.shuffle = random.Random(11)
        result, seen = sweep(board, pages=2, resync_budget=2, resync_slices=12, limit=80)
        self.assertTrue(result.complete, result)
        self.assertFalse(result.restart)
        self.assertEqual(seen, board.ids())

    def test_group_checkpoint_survives_json_and_throttling_without_exceeding_two_requests(self):
        board = Board(45)
        board.jobs = [(1000, NOW)] + [(1000 + i, NOW - 1) for i in range(1, 26)] + board.jobs[26:]
        board.shuffle = random.Random(11)
        a = adapter(board, max_pages_per_run=2, resync_budget=2, resync_slices=12)
        cur, seen, throttled = None, set(), False
        for _ in range(80):
            if cur and cur.get("group") and not throttled:
                board.throttle_at = {board.http.requests + 1}
                throttled = True
            else:
                board.throttle_at = set()
            before = board.http.requests
            result = a.fetch(FetchContext(cursor=cur, now=NOW))
            self.assertLessEqual(board.http.requests - before, 2)
            self.assertFalse(result.restart)
            seen.update(p.job_id for p in result.postings)
            if result.complete:
                break
            cur = json.loads(json.dumps(result.cursor))
        else:
            self.fail("resumed group never completed")
        self.assertTrue(throttled)
        self.assertEqual(seen, board.ids())

    def test_group_checkpoint_rejects_count_changes(self):
        board = Board(45)
        board.jobs = [(1000, NOW)] + [(1000 + i, NOW - 1) for i in range(1, 26)] + board.jobs[26:]
        a = adapter(board, max_pages_per_run=2, resync_budget=2, resync_slices=12)
        cur = None
        for _ in range(40):
            result = a.fetch(FetchContext(cursor=cur, now=NOW))
            cur = result.cursor
            if cur and cur.get("group"):
                break
        else:
            self.fail("test did not reach a pending timestamp group")
        board.add(5000, NOW + 10)
        result = a.fetch(FetchContext(cursor=cur, now=NOW + 3600))
        self.assertTrue(result.restart)
        self.assertFalse(result.complete)
        self.assertEqual(result.postings, [])

    def test_group_scan_rejects_timestamp_inversion_below_group(self):
        board = Board(45)
        board.jobs = [(1000, NOW)] + [(1000 + i, NOW - 1) for i in range(1, 26)] + board.jobs[26:]
        a = adapter(board, max_pages_per_run=2, resync_budget=2, resync_slices=12)
        cur = None
        for _ in range(40):
            result = a.fetch(FetchContext(cursor=cur, now=NOW))
            cur = result.cursor
            if cur and cur.get("group"):
                break
        else:
            self.fail("test did not reach group scan")
        board.jobs[26], board.jobs[27] = board.jobs[27], board.jobs[26]
        result = a.fetch(FetchContext(cursor=cur, now=NOW + 3600))
        self.assertTrue(result.restart)
        self.assertFalse(result.complete)

    def test_shared_allowance_bounds_initial_slice_and_resync(self):
        board = Board(100)
        a = adapter(board, max_pages_per_run=20, resync_budget=12)
        a.run_request_limit = 3
        first = a.fetch(FetchContext(now=NOW))
        self.assertFalse(first.complete)
        self.assertEqual(a.run_requests, 3)
        board.add(5000, NOW + 10)
        before = board.http.requests
        second = a.fetch(FetchContext(cursor=first.cursor, now=NOW + 3600))
        self.assertFalse(second.complete)
        self.assertLessEqual(board.http.requests - before, 3)
        self.assertEqual(a.run_requests, board.http.requests - before)
        self.assertEqual(a.resync_budget(), 3)

    def test_static_board_completes_with_one_head_check(self):
        board = Board(45)
        result, seen = sweep(board, pages=100)
        self.assertTrue(result.complete)
        self.assertEqual(seen, board.ids())
        self.assertEqual(board.http.requests, 5 + 1)

    def test_front_insertions_between_slices_do_not_restart(self):
        board = Board(60)
        new = iter(range(5000, 5100))

        def between(n):
            for _ in range(4):
                board.add(next(new), NOW + 1000 + n * 10 + _)

        result, seen = sweep(board, between)
        self.assertTrue(result.complete, result)
        self.assertFalse(result.restart)
        self.assertTrue({str(1000 + i) for i in range(60)} <= seen)
        self.assertEqual(seen, board.ids())     # head re-read also caught the new ones

    def test_closures_before_the_resume_point_skip_nothing(self):
        board = Board(60)

        def between(n):
            if n == 0:
                for job in (1002, 1005, 1011, 1020):   # all already read
                    board.remove(job)

        result, seen = sweep(board, between)
        self.assertTrue(result.complete, result)
        self.assertTrue(board.ids() <= seen)

    def test_tie_at_the_boundary_is_read_from_above(self):
        board = Board(0)
        board.jobs = [(1000 + i, NOW - (i // 4) * 60) for i in range(60)]  # blocks of four ties

        def between(n):
            if n == 0:
                board.remove(1001)                       # read in slice 0
                board.remove(1002)
                board.add(7000, NOW + 500)               # net shift -1 inside a tie block

        result, seen = sweep(board, between)
        self.assertTrue(result.complete, result)
        self.assertTrue(board.ids() <= seen)

    def test_large_unstable_same_timestamp_group_is_read_by_count(self):
        # Qualcomm, 2026-09-22: a batch of postings shares one postedTs and the
        # server returns the batch in a different order on every request.
        board = Board(80)
        board.jobs = [(i, ts if not 1025 <= i < 1048 else NOW - 25 * 60) for i, ts in board.jobs]
        board.sort()
        board.shuffle = random.Random(7)
        new = iter(range(5000, 5100))

        def between(n):
            board.add(next(new), NOW + 1000 + n)
            if n == 1:
                board.remove(1003)

        result, seen = sweep(board, between)
        self.assertTrue(result.complete, result)
        self.assertTrue(board.ids() <= seen)

    def test_throttle_in_the_middle_of_a_resync_cannot_skip_rows(self):
        board = Board(60)
        a = adapter(board, max_pages_per_run=3)
        first = a.fetch(FetchContext(now=NOW))
        for job in (1002, 1005):
            board.remove(job)                            # the list moves up by two
        board.throttle_at = {board.http.requests + 2}    # the re-sync's first request
        paused = a.fetch(FetchContext(cursor=first.cursor, now=NOW))
        self.assertIsNotNone(paused.throttled)
        self.assertEqual(paused.cursor["phase"], "resync")
        board.throttle_at = set()
        cursor, seen = paused.cursor, {p.job_id for p in first.postings}
        for _ in range(20):
            result = a.fetch(FetchContext(cursor=cursor, now=NOW))
            seen |= {p.job_id for p in result.postings}
            if result.complete or result.restart:
                break
            cursor = result.cursor
        self.assertTrue(result.complete, result)
        self.assertTrue(board.ids() <= seen)

    def test_group_whose_members_never_all_appear_restarts(self):
        board = Board(60)
        board.jobs = [(i, ts if not 1025 <= i < 1045 else NOW - 25 * 60) for i, ts in board.jobs]
        board.sort()
        board.hide = 1040
        result, seen = sweep(board, lambda n: board.remove(1001) if n == 0 else None)
        self.assertTrue(result.restart, result)

    def test_older_job_relisted_behind_the_boundary_restarts(self):
        board = Board(60)

        def between(n):
            if n == 0:
                board.add(9000, board.jobs[29][1] + 1)   # old timestamp, just above the anchor
                board.remove(1040)

        result, _ = sweep(board, between)
        self.assertTrue(result.restart)
        self.assertEqual(result.cursor["start"], 0)
        self.assertEqual(result.cursor["mode"], "anchor")

    def test_job_reposted_to_the_front_is_caught_by_the_head_check(self):
        board = Board(60)

        def between(n):
            if n == 0:
                board.remove(1055)                       # not read yet...
                board.add(1055, NOW + 900)               # ...reappears as the newest

        result, seen = sweep(board, between)
        self.assertTrue(result.complete, result)
        self.assertIn("1055", seen)
        self.assertEqual(seen, board.ids())

    def test_rows_out_of_timestamp_order_restart(self):
        board = Board(30)
        board.jobs[3], board.jobs[4] = board.jobs[4], board.jobs[3]
        result, _ = sweep(board, pages=100)
        self.assertTrue(result.restart)

    def test_throttle_keeps_the_anchored_cursor(self):
        board = Board(60)
        board.throttle_at = {5}                          # the fifth request
        a = adapter(board, max_pages_per_run=10)
        result = a.fetch(FetchContext(now=NOW))
        self.assertIsNotNone(result.throttled)
        self.assertEqual(result.cursor["start"], 40)
        self.assertEqual(len(result.cursor["ids"]), 40)
        self.assertEqual(result.cursor["mode"], "anchor")

    def test_offset_cursor_with_progress_restarts_but_zero_cursor_is_reused(self):
        board = Board(15)
        old = {"loc": 0, "start": 10, "ids": ["1"], "loc_ids": ["1"], "total": 15}
        self.assertTrue(adapter(board).fetch(FetchContext(cursor=old, now=NOW)).restart)
        zero = {"loc": 0, "start": 0, "ids": [], "loc_ids": [], "total": None}
        self.assertTrue(adapter(board).fetch(FetchContext(cursor=zero, now=NOW)).complete)

    def test_requires_one_location_and_timestamp_order(self):
        with self.assertRaisesRegex(AdapterError, "exactly one location"):
            adapter(Board(1), locations=["India", "Singapore"]).fetch(FetchContext(now=NOW))
        with self.assertRaisesRegex(AdapterError, "timestamp order"):
            adapter(Board(1), incremental=False).fetch(FetchContext(now=NOW))


if __name__ == "__main__":
    unittest.main()
