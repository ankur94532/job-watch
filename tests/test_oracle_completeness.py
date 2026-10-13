"""Oracle inventory validation, resumability, and Uber's location shape."""

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.adapters.phenom import PhenomAdapter
from watcher.http import HttpStatusError


CFG = {"ats": "oracle", "host": "oracle.example", "site": "CX_1", "locations": ["India"]}


def row(jid, **fields):
    return dict({"Id": str(jid), "Title": "Software Engineer", "PrimaryLocation": "Bengaluru, India"}, **fields)


def page(count, *rows):
    return {"items": [{"TotalJobsCount": count, "requisitionList": list(rows)}]}


def adapter(pages, cfg=None):
    def handle(method, url, payload, cache):
        finder = parse_qs(urlsplit(url).query)["finder"][0]
        offset = int(finder.split("offset=")[1].split(",")[0])
        response = pages[offset]
        if isinstance(response, Exception):
            raise response
        return response
    http = FakeHttp({lambda m, u, p: True: handle})
    return build_adapter("uber", cfg or CFG, http=http, page_delay=0), http


class _FakePost:
    """Serves one Phenom page per POST, in order."""

    def __init__(self, pages):
        self.pages, self.n = pages, 0

    def post_json(self, url, payload):
        rows, total = self.pages[min(self.n, len(self.pages) - 1)]
        self.n += 1
        return {"refineSearch": {"totalHits": total, "data": {"jobs": rows}}}


class OracleCompleteness(unittest.TestCase):
    def test_secondary_name_preserves_other_locations(self):
        a, _ = adapter({0: page(1, row(1, PrimaryLocation="London, United Kingdom",
                                      secondaryLocations=[{"Name": "Bengaluru, India", "CountryCode": "IN"}]))})
        with patch("watcher.adapters.oracle.log.warning") as warning:
            self.assertEqual(a.fetch().postings[0].location, "London, United Kingdom | Bengaluru, India")
        warning.assert_not_called()

    def test_count_drift_or_page_overlap_restarts_without_rows(self):
        for second in (page(3, row(2)), page(2, row(1))):
            with self.subTest(second=second):
                a, _ = adapter({0: page(2, row(1)), 1: second})
                result = a.fetch()
                self.assertTrue(result.restart)
                self.assertFalse(result.complete)
                self.assertEqual(result.postings, [])

    def test_invalid_pages_cannot_close_jobs(self):
        for pages in ({0: page(None, row(1))}, {0: page(True, row(1))},
                      {0: page(2, row(1), row(1))}, {0: page(1, row(1), row(2))},
                      {0: page(2, row(1)), 1: page(2)},
                      {0: page(1, row(1, Id=None))}, {0: page(1, row(1, Title=""))},
                      {0: page(1, row(1, PrimaryLocation=""))}):
            with self.subTest(pages=pages):
                a, _ = adapter(pages)
                with self.assertRaises(AdapterError):
                    a.fetch()

    def test_explicit_zero_count_is_a_complete_empty_inventory(self):
        a, _ = adapter({0: page(0)})
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings, [])

    def test_legacy_cursor_restarts_without_http(self):
        a, http = adapter({})
        result = a.fetch(FetchContext(cursor={"loc": 0, "offset": 1, "ids": ["1"]}))
        self.assertTrue(result.restart)
        self.assertEqual(http.calls, [])

    def test_deadline_persists_count_and_ids_then_resumes(self):
        a, _ = adapter({0: page(2, row(1)), 1: page(2, row(2))})
        with patch("watcher.adapters.oracle.time.monotonic", return_value=11):
            first = a.fetch(FetchContext(deadline=10))
        self.assertEqual(first.cursor["total"], 2)
        self.assertEqual(first.cursor["loc_ids"], ["1"])
        second = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(second.complete)
        self.assertEqual([p.job_id for p in second.postings], ["2"])

    def test_partial_throttle_keeps_paid_for_rows_and_resume_offset(self):
        err = HttpStatusError("https://oracle.example", 429, retry_after=1800)
        a, _ = adapter({0: page(2, row(1)), 1: err})
        result = a.fetch()
        self.assertIs(result.throttled, err)
        self.assertEqual([p.job_id for p in result.postings], ["1"])
        self.assertEqual(result.cursor["offset"], 1)
        self.assertFalse(result.complete)

    def test_locations_are_deduplicated_only_across_queries(self):
        a, _ = adapter({0: page(1, row(1))}, dict(CFG, locations=["India", "Bengaluru"]))
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 1)

    def test_disjoint_facets_replace_unstable_offset_pagination(self):
        cfg = dict(CFG, workplace_types=["ON", "HYBRID"], posting_date_facets=[30, 31])

        def response(method, url, payload, cache):
            finder = parse_qs(urlsplit(url).query)["finder"][0]
            if "limit=1" in finder:
                item = page(4)["items"][0]
                item.update({
                    "workplaceTypesFacet": [
                        {"Id": "ON", "TotalCount": 2},
                        {"Id": "HYBRID", "TotalCount": 2},
                    ],
                    # The overlapping seven-day shortcut may be advertised too;
                    # only the configured 30/31 disjoint cover is summed.
                    "postingDatesFacet": [
                        {"Id": 7, "TotalCount": 1},
                        {"Id": 30, "TotalCount": 2},
                        {"Id": 31, "TotalCount": 2},
                    ],
                })
                return {"items": [item]}
            fields = dict(part.split("=", 1) for part in finder.split(",") if "=" in part)
            work, date = fields["selectedWorkplaceTypesFacet"], fields["selectedPostingDatesFacet"]
            jid = {("ON", "30"): 1, ("ON", "31"): 2,
                   ("HYBRID", "30"): 3, ("HYBRID", "31"): 4}[(work, date)]
            item = page(1, row(jid))["items"][0]
            item.update({"SelectedWorkplaceTypesFacet": work,
                         "SelectedPostingDatesFacet": date})
            return {"items": [item]}

        http = FakeHttp({lambda m, u, p: True: response})
        a = build_adapter("honeywell", cfg, http=http, page_delay=0)
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["1", "2", "3", "4"])
        self.assertEqual(http.requests, 6)       # metadata + four partitions + final metadata

    def test_partition_metadata_must_prove_an_exact_cover(self):
        cfg = dict(CFG, workplace_types=["ON", "HYBRID"], posting_date_facets=[30, 31])
        item = page(4)["items"][0]
        item.update({
            "workplaceTypesFacet": [{"Id": "ON", "TotalCount": 2},
                                      {"Id": "HYBRID", "TotalCount": 1}],
            "postingDatesFacet": [{"Id": 30, "TotalCount": 2},
                                   {"Id": 31, "TotalCount": 2}],
        })
        http = FakeHttp({lambda m, u, p: True: {"items": [item]}})
        a = build_adapter("honeywell", cfg, http=http, page_delay=0)
        with self.assertRaisesRegex(AdapterError, "cover sums"):
            a.fetch()

    def test_partition_tolerance_accepts_only_a_terminal_boundary_duplicate(self):
        cfg = dict(CFG, workplace_types=["ON"], posting_date_facets=[30],
                   count_tolerance=1)

        def response(method, url, payload, cache):
            finder = parse_qs(urlsplit(url).query)["finder"][0]
            if "limit=1" in finder:
                item = page(2)["items"][0]
                item.update({
                    "workplaceTypesFacet": [{"Id": "ON", "TotalCount": 2}],
                    "postingDatesFacet": [{"Id": 30, "TotalCount": 2}],
                })
                return {"items": [item]}
            fields = dict(part.split("=", 1) for part in finder.split(",") if "=" in part)
            # Oracle exposes the same final boundary row twice while declaring
            # one additional inaccessible row.
            item = page(2, row(1))["items"][0]
            item.update({"SelectedWorkplaceTypesFacet": "ON",
                         "SelectedPostingDatesFacet": "30"})
            self.assertIn(fields["offset"], ("0", "1"))
            return {"items": [item]}

        http = FakeHttp({lambda m, u, p: True: response})
        a = build_adapter("honeywell", cfg, http=http, page_delay=0)
        with patch("watcher.adapters.oracle.log.warning") as warning:
            result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["1"])
        self.assertIn("boundary duplicate", warning.call_args[0][0])


class OracleCountTolerance(unittest.TestCase):
    """Oracle's OWN tenant over-declares TotalJobsCount by exactly one.

    Every one of the 14 failures on 2026-09-20 had a shortfall of 1, across two
    different totals (47 and 44), and the sweep therefore never completed, so
    closed requisitions were never retired. The allowance is opt-in per company
    and bounded; it must not become a blanket relaxation.
    """

    def test_default_is_strict_so_other_oracle_boards_are_unaffected(self):
        a, _ = adapter({0: page(3, row(1), row(2)), 2: page(3)})
        with self.assertRaises(AdapterError) as e:
            a.fetch()
        self.assertIn("incomplete Oracle page at 2/3", str(e.exception))

    def test_tolerated_shortfall_completes_the_sweep(self):
        cfg = dict(CFG, count_tolerance=1)
        a, _ = adapter({0: page(3, row(1), row(2)), 2: page(3)}, cfg)
        with patch("watcher.adapters.oracle.log.warning") as warning:
            result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["1", "2"])
        # The allowance is deliberately loud.
        self.assertTrue(warning.called)
        self.assertIn("shortfall", warning.call_args[0][0])

    def test_shortfall_larger_than_the_tolerance_still_fails(self):
        cfg = dict(CFG, count_tolerance=1)
        a, _ = adapter({0: page(4, row(1), row(2)), 2: page(4)}, cfg)
        self.assertRaises(AdapterError, a.fetch)

    def test_tolerance_never_excuses_a_page_exceeding_the_total(self):
        """Over-delivery is a different fault and no tolerance applies to it."""
        cfg = dict(CFG, count_tolerance=5)
        a, _ = adapter({0: page(1, row(1), row(2))}, cfg)
        self.assertRaises(AdapterError, a.fetch)

    def test_exact_count_still_completes_without_warning(self):
        cfg = dict(CFG, count_tolerance=1)
        a, _ = adapter({0: page(2, row(1), row(2)), 2: page(2)}, cfg)
        with patch("watcher.adapters.oracle.log.warning") as warning:
            self.assertTrue(a.fetch().complete)
        warning.assert_not_called()

    def test_bad_tolerance_is_refused(self):
        for bad in (-1, "1", 1.5, True):
            with self.subTest(bad=bad):
                a, _ = adapter({0: page(0)}, dict(CFG, count_tolerance=bad))
                self.assertRaises(AdapterError, a.fetch)


@patch.object(PhenomAdapter, "check_robots", create=True)
class PhenomRepeatedRows(unittest.TestCase):
    """A facet may serve every slot it declares while repeating one of its rows.

    Measured on IDFC FIRST 2026-10-14: the failing facet served all 235 slots
    and held 230 distinct ids, and two consecutive passes returned an IDENTICAL
    set. Replays cannot fill a stable repeat, so the board had failed closed
    since 20 September and never retired a closed role.
    """

    def jobs(self, ids):
        return [{"jobId": str(i), "title": "Software Engineer", "cityState": "Bengaluru",
                 "country": "India", "jobSeqNo": str(i), "applyUrl": f"https://x/{i}"}
                for i in ids]

    def adapter(self, pages, **cfg):
        a = PhenomAdapter("q", dict({"host": "careers.example.com"}, **cfg), page_delay=0)
        a.http = _FakePost(pages)
        return a

    def test_repeat_within_tolerance_completes(self, _):
        a = self.adapter([(self.jobs([1, 2, 2]), 3)], repeat_tolerance=1)
        r = a.fetch()
        self.assertTrue(r.complete)
        self.assertEqual(sorted(p.job_id for p in r.postings), ["1", "2"])

    def test_without_tolerance_it_still_fails(self, _):
        a = self.adapter([(self.jobs([1, 2, 2]), 3)])
        self.assertRaises(AdapterError, a.fetch)

    def test_a_short_board_still_fails_even_with_tolerance(self, _):
        """Fewer SLOTS than declared is a different fault and is never forgiven."""
        a = self.adapter([(self.jobs([1, 2]), 5)], repeat_tolerance=5)
        self.assertRaises(AdapterError, a.fetch)
