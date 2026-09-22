import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from tests.test_followup_boards import siemens_page, siemens_detail
from watcher.adapters import AdapterError, FetchContext
from watcher.adapters.siemens import SiemensAdapter, BASE
from watcher.filters import filter_for


@patch.object(SiemensAdapter, 'check_robots')
class SiemensFacetSafety(unittest.TestCase):
    def test_old_order_cursor_restarts_before_any_request(self, robots):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        adapter.http = FakeHttp({})
        result = adapter.fetch(FetchContext(cursor={
            'offset': 1, 'total': 2, 'pending': [], 'ids': ['1']}))
        self.assertTrue(result.restart)
        self.assertFalse(result.complete)
        self.assertEqual(result.postings, [])
        self.assertEqual(result.cursor['offset'], 0)
        self.assertEqual(result.cursor['ids'], [])
        robots.assert_not_called()

    def test_ignored_or_changed_sort_cannot_complete(self, _):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        page = siemens_page([1])
        for wrong in (page.replace('"ASC"', '"DESC"'),
                      page.replace('"postedDate"', 'null'),
                      page.replace('"jobSort"', '"unrelated"')):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(AdapterError, 'ordering'):
                adapter.parse_listing(wrong, 0)

    def test_non_india_detail_counts_toward_complete_sweep_but_does_not_match(self, _):
        adapter = SiemensAdapter('siemens', {'max_requests_per_run': 1}, page_delay=0)
        detail = siemens_detail(516009).replace(
            'Bangalore - Karnataka - India', 'Toronto - Ontario - Canada').replace(
            'Munich - Germany', 'Troy - Michigan - United States of America')
        adapter.http = FakeHttp({
            adapter.url(0): siemens_page([516009, 2], multiple=True),
            BASE + 'JobDetail/516009': detail,
            BASE + 'JobDetail/2': siemens_detail(2),
        })
        first = adapter.fetch()
        second = adapter.fetch(FetchContext(cursor=first.cursor))
        self.assertFalse(second.complete)
        self.assertEqual(second.cursor['ids'], ['516009'])
        self.assertEqual(second.cursor['pending'][0]['job_id'], '2')
        final = adapter.fetch(FetchContext(cursor=second.cursor))
        self.assertTrue(final.complete)
        self.assertEqual(len(second.postings) + len(final.postings), 2)
        job = second.postings[0]
        self.assertEqual(job.location, 'Toronto - Ontario - Canada | Troy - Michigan - United States of America')
        india = filter_for({'location': ['india']}, {'title': ['software engineer']})
        self.assertFalse(india.matches(job))
        self.assertTrue(india.matches(final.postings[0]))

    def test_missing_detail_location_still_fails(self, _):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        adapter.http = FakeHttp({
            adapter.url(0): siemens_page([1], multiple=True),
            BASE + 'JobDetail/1': siemens_detail(1).replace('<li>', '<span>').replace('</li>', '</span>'),
        })
        with self.assertRaisesRegex(AdapterError, 'missing Siemens detail locations'):
            adapter.fetch()

    def test_known_multi_location_job_does_not_refetch_its_detail(self, _):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        adapter.http = FakeHttp({
            adapter.url(0): siemens_page([1], multiple=True),
        })
        result = adapter.fetch(FetchContext(known={'1'}))
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ['1'])
        self.assertEqual(adapter.http.requests, 1)

    def test_structured_plain_location_keeps_global_scope(self, _):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        detail = (siemens_detail(493275).split(
            '<div class="article__content__view__field__label">Location(s)</div>')[0] +
            '<div class="article__content__view__field__value"><div><strong>Location:</strong>'
            '<span>Any Siemens location in the world</span></div></div>')
        self.assertEqual(adapter.detail_location(detail, '493275'),
                         'Any Siemens location in the world')
        india = filter_for({'location': ['india']}, {'title': []})
        self.assertFalse(india.location_ok('Any Siemens location in the world'))

    def test_count_decrease_and_page_overlap_restart_the_sweep(self, _):
        cases = (
            (siemens_page([1], total=3), siemens_page([2], offset=1, total=2)),
            (siemens_page([1], total=2), siemens_page([1], offset=1, total=2)),
        )
        for first_page, changed_page in cases:
            with self.subTest(changed_page=changed_page):
                adapter = SiemensAdapter('siemens', {'max_requests_per_run': 1}, page_delay=0)
                adapter.http = FakeHttp({
                    adapter.url(0): first_page,
                    adapter.url(1): changed_page,
                })
                first = adapter.fetch()
                self.assertEqual(first.cursor['offset'], 1)
                restarted = adapter.fetch(FetchContext(cursor=first.cursor))
                self.assertFalse(restarted.complete)
                self.assertTrue(restarted.restart)
                self.assertEqual(restarted.postings, [])
                self.assertEqual(restarted.cursor['offset'], 0)
                self.assertEqual(restarted.cursor['ids'], [])

    def test_count_increase_extends_oldest_first_sweep(self, _):
        adapter = SiemensAdapter('siemens', {'max_requests_per_run': 1}, page_delay=0)
        adapter.http = FakeHttp({
            adapter.url(0): siemens_page([1], total=2),
            adapter.url(1): siemens_page([2], offset=1, total=3),
            adapter.url(2): siemens_page([3], offset=2, total=3),
        })

        first = adapter.fetch()
        grown = adapter.fetch(FetchContext(cursor=first.cursor))
        self.assertFalse(grown.complete)
        self.assertFalse(grown.restart)
        self.assertEqual(grown.cursor['total'], 3)
        self.assertEqual(grown.cursor['offset'], 2)

        final = adapter.fetch(FetchContext(cursor=grown.cursor))
        self.assertTrue(final.complete)
        self.assertEqual([p.job_id for p in first.postings + grown.postings + final.postings],
                         ['1', '2', '3'])

    def test_off_host_job_link_is_not_accepted(self, _):
        adapter = SiemensAdapter('siemens', {}, page_delay=0)
        page = siemens_page([1]).replace(
            BASE + 'JobDetail/1', 'https://example.com/en_US/externaljobs/JobDetail/1')
        with self.assertRaisesRegex(AdapterError, 'missing/ambiguous Siemens job link'):
            adapter.parse_listing(page, 0)
