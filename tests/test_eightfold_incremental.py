"""Eightfold incremental polls: pcsx sorted by timestamp, newest first.

Microsoft's quota runs out a few requests into a 23-request sweep, so a normal
poll must be the newest pages only, and must never pretend to be a sweep.
"""

import unittest
from urllib.parse import parse_qs, urlparse

from tests.fakes import FakeHttp
from watcher.adapters import FetchContext, build_adapter
from watcher.adapters import eightfold as ef

NOW = 1_800_000_000
CFG = {"ats": "eightfold", "host": "ms.example", "domain": "ms.com", "locations": ["India"],
       "incremental": True, "page_delay": 0}


def board(n, stamps=None, sort_echo="timestamp"):
    """n positions, newest first unless `stamps` says otherwise."""
    stamps = stamps or [NOW - i * 3600 for i in range(n)]

    def handler(method, url, payload, cache):
        q = parse_qs(urlparse(url).query)
        start = int(q["start"][0])
        rows = [{"id": 1000 + i, "name": f"Software Engineer {i}", "locations": ["Bengaluru, India"],
                 "postedTs": stamps[i]} for i in range(start, min(start + 10, n))]
        return {"data": {"count": n, "sortBy": sort_echo, "positions": rows}}
    return FakeHttp({lambda m, u, p: "/api/" in u: handler})


def ctx(known, swept_ago=3600):
    return FetchContext(known={str(k) for k in known}, last_complete=NOW - swept_ago, now=NOW)


def adapter(http, **cfg):
    return build_adapter("microsoft", dict(CFG, **cfg), http=http, page_delay=0)


class Incremental(unittest.TestCase):
    def test_a_normal_poll_is_three_requests_not_a_sweep(self):
        http = board(230)
        known = range(1003, 1230)                               # 1000-1002 are new
        res = adapter(http).fetch(ctx(known))
        self.assertTrue(res.incremental)
        self.assertFalse(res.complete)
        self.assertEqual(len(http.calls), 3)                    # the page with news, then two known
        self.assertTrue({"1000", "1001", "1002"} <= {p.job_id for p in res.postings})

    def test_it_asks_for_the_timestamp_order(self):
        http = board(30)
        adapter(http).fetch(ctx(range(1000, 1030)))
        self.assertIn("sort_by=timestamp", http.calls[0][1])

    def test_an_order_that_goes_up_falls_back_to_a_sweep(self):
        stamps = [NOW - i * 3600 for i in range(40)]
        stamps[15] = NOW + 99                                   # newer than what came before it
        res = adapter(board(40, stamps)).fetch(ctx(range(1000, 1040)))
        self.assertFalse(res.incremental)
        self.assertTrue(res.complete)

    def test_a_server_that_ignores_the_sort_falls_back_to_a_sweep(self):
        res = adapter(board(40, sort_echo="distance")).fetch(ctx(range(1000, 1040)))
        self.assertTrue(res.complete)

    def test_a_full_sweep_still_runs_every_full_sweep(self):
        res = adapter(board(40)).fetch(ctx(range(1000, 1040), swept_ago=ef.FULL_SWEEP + 1))
        self.assertTrue(res.complete)

    def test_the_apply_flavour_never_goes_incremental(self):
        """apply/v2 accepts sort_by and returns rows unordered (NetApp, 2026-09-18)."""
        a = adapter(board(20), api="apply")
        self.assertFalse(a.incremental)
        self.assertNotIn("sort_by", a.url("India", 0))


if __name__ == "__main__":
    unittest.main()
