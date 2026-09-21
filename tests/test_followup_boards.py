import json
import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.adapters.bamboohr import BambooHRAdapter
from watcher.adapters.zwayam import ZwayamAdapter
from watcher.adapters.siemens import SiemensAdapter, BASE
from watcher.adapters.public_html import TrakstarAdapter, SuccessFactorsHTMLAdapter, CountedHTML
from watcher.filters import filter_for
from watcher.http import HttpStatusError


class AshbySafety(unittest.TestCase):
    def test_qdrant_secondary_india_and_restricted_remote(self):
        jobs = [{'id': 'apac', 'title': 'Senior Solutions Engineer (APAC)',
                 'location': 'Remote Australia', 'secondaryLocations': [{'location': 'Remote India'}],
                 'isRemote': True, 'jobUrl': 'https://jobs.ashbyhq.com/qdrant.tech/apac'},
                {'id': 'emea', 'title': 'Software Engineer', 'location': 'Remote EMEA',
                 'isRemote': True, 'jobUrl': 'https://jobs.ashbyhq.com/qdrant.tech/emea'}]
        url = 'https://api.ashbyhq.com/posting-api/job-board/qdrant.tech'
        a = build_adapter('qdrant', {'ats': 'ashby', 'token': 'qdrant.tech'}, http=FakeHttp({url: {'jobs': jobs}}))
        apac, emea = a.fetch().postings
        f = filter_for({'location': ['india']}, {'title': ['software engineer']})
        self.assertTrue(f.location_ok(apac.location))
        self.assertFalse(f.title_ok(apac.title))
        self.assertFalse(f.matches(emea))
        for bad in ({}, {'jobs': jobs + jobs}, {'jobs': [dict(jobs[0], location='', secondaryLocations=[])]}):
            a.http = FakeHttp({url: bad})
            with self.assertRaises(AdapterError):
                a.fetch()


@patch.object(BambooHRAdapter, 'check_robots')
class BambooSafety(unittest.TestCase):
    def job(self, **kw):
        return dict({'id': '36', 'jobOpeningName': 'Software Engineer',
                     'location': {'city': 'Trondheim', 'state': None},
                     'atsLocation': {'country': None}, 'isRemote': None}, **kw)

    def fetch(self, data):
        return BambooHRAdapter('vespa', {'host': 'vespaai.bamboohr.com'}, http=FakeHttp({
            'https://vespaai.bamboohr.com/careers/list': data})).fetch()

    def test_complete_and_explicit_empty(self, _):
        p = self.fetch({'meta': {'totalCount': 1}, 'result': [self.job()]}).postings[0]
        self.assertEqual(p.location, 'Trondheim')
        self.assertEqual(p.url, 'https://vespaai.bamboohr.com/careers/36')
        self.assertEqual(self.fetch({'meta': {'totalCount': 0}, 'result': []}).postings, [])

    def test_remote_does_not_remove_country_restriction(self, _):
        p = self.fetch({'meta': {'totalCount': 1}, 'result': [self.job(
            location={}, atsLocation={'country': 'United States'}, isRemote=True)]}).postings[0]
        self.assertTrue(p.remote)
        self.assertEqual(p.location, 'United States')
        self.assertFalse(filter_for({'location': ['india']}, {}).matches(p))

    def test_missing_duplicate_or_partial_fails(self, _):
        bad = [{}, {'meta': {'totalCount': 2}, 'result': [self.job()]},
               {'meta': {'totalCount': 2}, 'result': [self.job(), self.job()]},
               {'meta': {'totalCount': 1}, 'result': [self.job(location={}, atsLocation={})]}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(AdapterError):
                self.fetch(data)


def zj(i):
    return {'_source': {'id': i, 'jobTitle': 'Software Engineer', 'companyId': 16090,
                       'location': 'Gurugram - HQ', 'jobLocationRecord': [{'country': 'India'}],
                       'jobUrl': f'engineer-{i}', 'displayStatus': 'Open', 'refNumber': f'G-{i}'}}


def zp(ids, total, more):
    return {'data': [zj(i) for i in ids], 'totalCount': total, 'hasMoreData': more}


@patch.object(ZwayamAdapter, 'check_robots')
class ZwayamSafety(unittest.TestCase):
    def adapter(self, pages, **cfg):
        a = ZwayamAdapter('greyorange', dict(company_id='16090', host='careers.greyorange.com',
            board_path='greyorange', **cfg), page_delay=0)
        a.search = lambda offset: pages[offset] if not isinstance(pages[offset], Exception) else self.fail_with(pages[offset])
        return a

    def fail_with(self, e):
        raise e

    def test_resume_and_counts(self, _):
        a = self.adapter({0: zp([1, 2], 3, True), 2: zp([3], 3, False)}, max_pages_per_run=1)
        first = a.fetch()
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor['ids'], ['1', '2'])
        final = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(final.complete)
        self.assertEqual(final.postings[0].location, 'Gurugram - HQ | India')
        self.assertEqual(final.postings[0].req_id, 'G-3')

    def test_throttle_preserves_offset_even_on_resumed_first_request(self, _):
        for code in (403, 429):
            a = self.adapter({0: zp([1], 2, True), 1: HttpStatusError('u', code)})
            r = a.fetch()
            self.assertEqual(r.cursor['offset'], 1)
            self.assertEqual(r.throttled.code, code)
            again = a.fetch(FetchContext(cursor=r.cursor))
            self.assertEqual(again.cursor, r.cursor)
            a.search = lambda offset: zp([2], 2, False)
            self.assertTrue(a.fetch(FetchContext(cursor=again.cursor)).complete)

    def test_count_drift_and_duplicate_restart_without_closures(self, _):
        for tail in (zp([2], 3, True), zp([1], 2, False)):
            r = self.adapter({0: zp([1], 2, True), 1: tail}).fetch()
            self.assertFalse(r.complete)
            self.assertTrue(r.restart)
            self.assertIsNone(r.cursor)
            self.assertEqual(r.postings, [])

    def test_malformed_or_premature_end_fails(self, _):
        wrong = zj(1); wrong['_source']['companyId'] = 77
        for page in ({}, zp([], 1, True), zp([1], 2, False), zp([1], 1, True),
                     dict(zp([1], 1, False), data=[wrong])):
            with self.subTest(page=page), self.assertRaises(AdapterError):
                self.adapter({0: page}).fetch()

    def test_search_uses_read_only_multipart_frontend_fields(self, _):
        class HTTP:
            def request(self, method, url, data, headers):
                self.call = method, url, data, headers
                return json.dumps({'code': 200, 'data': zp([], 0, False)}).encode(), {}
        h = HTTP()
        a = ZwayamAdapter('greyorange', {'company_id': '16090', 'host': 'careers.greyorange.com',
                                      'board_path': 'greyorange'}, http=h)
        a.search(9)
        method, url, body, headers = h.call
        self.assertEqual((method, url), ('POST', 'https://public.zwayam.com/jobs/search'))
        self.assertIn(b'MTYwOTA=', body)
        self.assertIn(b'"paginationStartNo": 9', body)
        self.assertTrue(headers['Content-Type'].startswith('multipart/form-data;'))


def siemens_page(ids, offset=0, total=None, multiple=False):
    total = len(ids) if total is None else total
    last = offset + len(ids)
    return ('<script>var twigConfig = {"pageParameters": '
            '{"jobSort":"postedDate","jobSortDirection":"ASC"}};</script>' +
        f'<div class="list-controls__text__legend">{offset+1} - {last} of {total}</div>' +
        ''.join(f'<article class="article article--result"><h3><a href="{BASE}JobDetail/{i}">Software Engineer</a></h3>'
                f'<span class="list-item-jobCity">{"Multiple Locations" if multiple else "Bengaluru"}</span>' +
                ('' if multiple else '<span class="list-item-jobCountry">India</span>') +
                f'<span class="list-item-jobId">Job ID: {i}</span></article>' for i in ids) +
        (f'<a href="?folderOffset={last}">Next</a>' if last < total else ''))


def siemens_detail(i):
    return (f'<div class="article__content__view__field__label">Job ID</div>'
            f'<div class="article__content__view__field__value">{i}</div>'
            '<div class="article__content__view__field__label">Location(s)</div>'
            '<div class="article__content__view__field__value"><ul><li>Bangalore - Karnataka - India</li>'
            '<li>Munich - Germany</li></ul></div>')


@patch.object(SiemensAdapter, 'check_robots')
class SiemensSafety(unittest.TestCase):
    def adapter(self, **cfg):
        return SiemensAdapter('siemens', cfg, page_delay=0)

    def test_resume_pending_detail_and_pagination(self, _):
        a = self.adapter(max_requests_per_run=1)
        a.http = FakeHttp({a.url(0): siemens_page([1], total=2, multiple=True),
                           a.url(1): siemens_page([2], offset=1, total=2),
                           BASE+'JobDetail/1': siemens_detail(1)})
        first = a.fetch()
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor['ids'], [])
        self.assertEqual(len(first.cursor['pending']), 1)
        second = a.fetch(FetchContext(cursor=first.cursor))
        self.assertIn('Germany', second.postings[0].location)
        final = a.fetch(FetchContext(cursor=second.cursor))
        self.assertTrue(final.complete)
        self.assertEqual([p.job_id for p in final.postings], ['2'])
        self.assertEqual(a.http.requests, 3)

    def test_detail_throttle_keeps_pending_page(self, _):
        a = self.adapter()
        a.http = FakeHttp({a.url(0): siemens_page([1], multiple=True),
                           BASE+'JobDetail/1': HttpStatusError('u', 429)})
        r = a.fetch()
        self.assertFalse(r.complete)
        self.assertEqual(r.cursor['pending'][0]['job_id'], '1')
        a.http.routes[BASE+'JobDetail/1'] = siemens_detail(1)
        self.assertTrue(a.fetch(FetchContext(cursor=r.cursor)).complete)

    def test_missing_rows_count_identity_and_location_fail(self, _):
        a = self.adapter()
        good = siemens_page([1])
        for bad in ('<html>challenge</html>', good.replace('of 1', 'of 0'),
                    good.replace('Job ID: 1', 'Job ID: 2'),
                    good.replace('list-item-jobCity', 'unknown').replace('list-item-jobCountry', 'unknown')):
            with self.subTest(bad=bad), self.assertRaises(AdapterError):
                a.parse_listing(bad, 0)
        with self.assertRaises(AdapterError):
            a.detail_location(siemens_detail(2), '1')

    def test_next_link_required_and_short_last_page_accepted(self, _):
        a = self.adapter()
        with self.assertRaises(AdapterError):
            a.parse_listing(siemens_page([1], total=2).replace('folderOffset=1', 'broken'), 0)
        rows, total = a.parse_listing(siemens_page([955], offset=954, total=955), 954)
        self.assertEqual((len(rows), total), (1, 955))
        self.assertIn('India', a.detail_location(siemens_detail(1).replace(
            'Location(s)', 'Any Siemens location in'), '1'))


class EightfoldSafety(unittest.TestCase):
    def adapter(self, pages):
        a = build_adapter('hsbc', {'ats': 'eightfold', 'api': 'apply', 'host': 'portal.careers.hsbc.com',
                                  'domain': 'hsbc.com', 'locations': ['India']}, page_delay=0)
        a.http = FakeHttp({a.url('India', offset): data for offset, data in pages.items()})
        return a

    def page(self, ids, count):
        return {'count': count, 'positions': [{'id': i, 'name': 'Software Engineer', 'location': 'Pune, India'} for i in ids]}

    def test_missing_payload_cannot_confirm_empty(self):
        for page in ({}, {'count': 0}, {'positions': []}, self.page([], 1)):
            with self.subTest(page=page), self.assertRaises(AdapterError):
                self.adapter({0: page}).fetch()
        self.assertTrue(self.adapter({0: self.page([], 0)}).fetch().complete)

    def test_resume_throttle_and_count_drift(self):
        a = self.adapter({0: self.page([1], 2), 1: HttpStatusError('u', 403)})
        r = a.fetch()
        self.assertEqual(r.cursor['loc_ids'], ['1'])
        self.assertEqual(a.fetch(FetchContext(cursor=r.cursor)).cursor, r.cursor)
        a.http.routes[a.url('India', 1)] = self.page([2], 3)
        restart = a.fetch(FetchContext(cursor=r.cursor))
        self.assertFalse(restart.complete)
        self.assertTrue(restart.restart)
        self.assertIsNone(restart.cursor)


def trakstar_page(i=1, total=1, nxt=None):
    return (f'<h1 class="company-header-title">Jobs at BookMyShow</h1>'
            f"<script>paginator: {{ start_index: '{i}', end_index: '{i}', total_results: '{total}', "
            f"next_page_url: {repr(nxt) if nxt else 'null'} }}</script>"
            f'<div class="js-careers-page-job-list-item"><a href="/jobs/fk{i}/">'
            '<h3>Software Engineer</h3><div class="js-job-list-opening-loc" title="Mumbai, India">'
            'Mumbai, India</div></a></div>')


@patch.object(CountedHTML, 'check_robots')
class CountedHTMLSafety(unittest.TestCase):
    def adapter(self):
        return TrakstarAdapter('bookmyshow', {'host': 'bookmyshow.hire.trakstar.com',
            'organization': 'BookMyShow'}, page_delay=0)

    def test_resume_follows_published_next_url_and_validates_all_ids(self, _):
        a = self.adapter(); base = 'https://bookmyshow.hire.trakstar.com/'
        a.http = FakeHttp({base: trakstar_page(1, 2, '?page=2'), base+'?page=2': trakstar_page(2, 2)})
        first = a.fetch(FetchContext(deadline=0))
        self.assertFalse(first.complete)
        self.assertEqual(first.cursor['ids'], ['fk1'])
        final = a.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(final.complete)
        self.assertEqual(final.postings[0].job_id, 'fk2')

    def test_partial_missing_next_and_off_host_url_cannot_complete(self, _):
        a = self.adapter(); base = 'https://bookmyshow.hire.trakstar.com/'
        for page in (trakstar_page(1, 2), trakstar_page(1, 2, 'https://other.com/'),
                     trakstar_page().replace('Jobs at BookMyShow', 'Jobs at Someone Else'),
                     trakstar_page().replace('js-job-list-opening-loc', 'unknown')):
            a.http = FakeHttp({base: page})
            with self.subTest(page=page), self.assertRaises(AdapterError):
                a.fetch()

    def test_throttle_retains_validated_html_rows(self, _):
        a = self.adapter(); base = 'https://bookmyshow.hire.trakstar.com/'
        a.http = FakeHttp({base: trakstar_page(1, 2, '?page=2'), base+'?page=2': HttpStatusError('u', 429)})
        r = a.fetch()
        self.assertEqual(r.cursor['ids'], ['fk1'])
        self.assertEqual(r.throttled.code, 429)

    def test_count_drift_and_page_overlap_restart_without_rows(self, _):
        base = 'https://bookmyshow.hire.trakstar.com/'
        for second in (trakstar_page(2, 3, '?page=3'), trakstar_page(1, 2)):
            with self.subTest(second=second):
                a = self.adapter()
                a.http = FakeHttp({base: trakstar_page(1, 2, '?page=2'),
                                   base+'?page=2': second})
                r = a.fetch()
                self.assertFalse(r.complete)
                self.assertTrue(r.restart)
                self.assertIsNone(r.cursor)
                self.assertEqual(r.postings, [])

    def test_successfactors_count_and_restricted_remote(self, _):
        a = SuccessFactorsHTMLAdapter('chargebee', {'host': 'jobs.chargebee.com',
            'organization': 'Chargebee Technologies Private Limited'})
        page = ('<title>Chargebee Technologies Private Limited Jobs</title>'
                '<span class="paginationLabel">Results <b>1 – 1</b> of <b>1</b></span>'
                '<tr class="data-row"><a href="/job/Remote-Software-Engineer/123/">Software Engineer</a>'
                '<span class="jobLocation">Remote, US</span></tr>')
        a.http = FakeHttp({'https://jobs.chargebee.com/search/': page})
        r = a.fetch()
        self.assertTrue(r.complete)
        self.assertFalse(filter_for({'location': ['india']}, {}).matches(r.postings[0]))
        with self.assertRaises(AdapterError):
            a.parse(page.replace('paginationLabel', 'missing'))

    def test_successfactors_can_start_at_an_exact_filtered_search(self, _):
        page = ('<title>India - adidas Jobs</title>'
                '<span class="paginationLabel">Results 1 – 1 of 1</span>'
                '<tr class="data-row"><a href="/job/Bengaluru-Engineer/456/">Engineer</a>'
                '<span class="jobLocation">Bengaluru, India</span></tr>')
        path = '/search/?q=&locationsearch=India'
        a = SuccessFactorsHTMLAdapter('adidas', {'host': 'jobs.adidas-group.com',
            'organization': 'adidas', 'path': path})
        a.http = FakeHttp({'https://jobs.adidas-group.com' + path: page})
        result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.postings[0].job_id, '456')
        self.assertEqual(a.http.calls[0][1], 'https://jobs.adidas-group.com' + path)

    def test_counted_html_rejects_relative_configured_path(self, _):
        a = SuccessFactorsHTMLAdapter('x', {'host': 'example.com',
            'organization': 'x', 'path': 'search/?q=x'})
        with self.assertRaises(AdapterError):
            a.fetch()


if __name__ == '__main__':
    unittest.main()
