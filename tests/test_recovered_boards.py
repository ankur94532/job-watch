import json
import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.adapters.jobvite import JobviteAdapter
from watcher.adapters.navi import NaviAdapter
from watcher.adapters.schema_sitemap import SchemaSitemapAdapter
from watcher.http import HttpStatusError
from watcher.robots import Robots


def row(job_id="one", location="Bangalore, India"):
    return (f'<tr><td class="jv-job-list-name"><a href="/nutanix/job/{job_id}">'
            f'Software Engineer</a></td><td class="jv-job-list-location">{location}</td></tr>')


class JobviteTests(unittest.TestCase):
    def adapter(self, http=None):
        return JobviteAdapter("nutanix", {"token": "nutanix", "region": "India"}, http=http, page_delay=0)

    def test_ignores_featured_jobs_and_rejects_incomplete_rows(self):
        a = self.adapter()
        page = '<a href="/nutanix/job/featured">Featured US Job</a><table class="jv-job-list">' + row() + '</table>'
        self.assertEqual([r['job_id'] for r in a.parse_listing(page)], ['one'])
        with self.assertRaises(AdapterError):
            a.parse_listing(page.replace('jv-job-list-location', 'new-location-class'))
        with self.assertRaises(AdapterError):
            a.parse_listing(page + '<div class="jv-pagination">Next</div>')

    @patch.object(JobviteAdapter, 'check_robots')
    def test_multiple_locations_resolved_before_filtering_and_resume(self, _):
        listing = '<table class="jv-job-list">' + row('one') + row('two', '2 Locations') + '</table>'
        detail = ('<p class="jv-job-detail-meta">Engineering<span class="jv-inline-separator"></span>'
                  'San Jose, California<span class="jv-inline-separator"></span>Bangalore, India'
                  '<span class="jv-inline-separator"></span>Req.Num.: 1234</p>')
        http = FakeHttp({'https://jobs.jobvite.com/nutanix/jobs?r=India': listing,
                         'https://jobs.jobvite.com/nutanix/job/two': detail})
        a = self.adapter(http)
        partial = a.fetch(FetchContext(deadline=0))
        self.assertFalse(partial.complete)
        self.assertEqual(partial.cursor['ids'], ['one'])
        result = a.fetch(FetchContext(cursor=partial.cursor))
        self.assertTrue(result.complete)
        self.assertEqual(result.postings[0].location, 'San Jose, California | Bangalore, India')
        self.assertEqual(result.postings[0].req_id, '1234')
        self.assertEqual(http.requests, 2)


@patch.object(NaviAdapter, 'check_robots')
class NaviTests(unittest.TestCase):
    def fetch(self, data):
        return NaviAdapter('navi', {}, http=FakeHttp({
            'https://navi.com/api/careers/jobs': data}), page_delay=0).fetch()

    def job(self):
        return {'JobId': 'abc', 'JobTitle': 'Software Engineer', 'JobCode': 'N-123',
                'CompanyUrl': 'http://navi.com/', 'Location': json.dumps([
                    {'Address': 'Bangalore, India'}, {'Address': 'Mumbai, India'}]),
                'ApplyUrl': 'https://navi.turbohire.co/job/publicjobs/abc?utm_source=CareerPage',
                'PublishedDate': '2026-09-18T10:00:00Z'}

    def test_complete_feed_and_explicit_empty(self, _):
        p = self.fetch({'Total': 1, 'FilteredCount': 1, 'Jobs': [self.job()]}).postings[0]
        self.assertEqual(p.location, 'Bangalore, India | Mumbai, India')
        self.assertEqual(p.req_id, 'N-123')
        self.assertEqual(p.posted_at, '2026-09-18')
        self.assertEqual(self.fetch({'Total': 0, 'FilteredCount': 0, 'Jobs': []}).postings, [])

    def test_missing_data_bad_counts_duplicate_and_wrong_identity_fail(self, _):
        bad = [{}, {'Total': 2, 'FilteredCount': 1, 'Jobs': [self.job()]},
               {'Total': 2, 'FilteredCount': 2, 'Jobs': [self.job(), self.job()]}]
        for field, value in [('CompanyUrl', 'https://unrelated.com'), ('Location', '2 Locations'),
                             ('Location', '[]'), ('ApplyUrl', 'https://evil.com/job/abc'),
                             ('JobTitle', '')]:
            job = self.job(); job[field] = value
            bad.append({'Total': 1, 'FilteredCount': 1, 'Jobs': [job]})
        for data in bad:
            with self.subTest(data=data), self.assertRaises(AdapterError):
                self.fetch(data)


SM = 'https://www.metacareers.com/jobsearch/sitemap.xml'
BASE = 'https://www.metacareers.com/profile/job_details/'
CFG = {'ats': 'schema_sitemap', 'sitemap': SM, 'organization': 'Meta',
       'job_url': r'https://www\.metacareers\.com/profile/job_details/(?P<req>\d+)/'}


def sitemap(*ids):
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            ''.join(f'<url><loc>{BASE}{i}/</loc></url>' for i in ids) + '</urlset>').encode()


def detail(company='Meta', locations=None):
    return '<script type="application/ld+json">' + json.dumps({
        '@type': 'JobPosting', 'title': 'Software Engineer', 'hiringOrganization': {'name': company},
        'jobLocation': locations or [{'address': {'addressLocality': 'Bengaluru', 'addressCountry': 'IN'}},
                                    {'address': {'addressLocality': 'London', 'addressCountry': 'GB'}}],
    }) + '</script>'


class SitemapHttp(FakeHttp):
    def request(self, method, url, headers=None):
        return self._respond(method, url), {}


@patch('watcher.adapters.schema_sitemap.fetch_robots', return_value=Robots(''))
class SchemaSitemapTests(unittest.TestCase):
    def test_cache_avoids_refetch_and_new_jobs_are_found(self, _):
        http = SitemapHttp({SM: sitemap(1), BASE+'1/': detail()})
        a = build_adapter('meta', CFG, http=http, page_delay=0)
        ctx = FetchContext(now=100)
        self.assertEqual(len(a.fetch(ctx).postings), 1)
        self.assertIn('India', a.fetch(ctx).postings[0].location)
        self.assertEqual(http.requests, 3)  # two sitemaps, one detail
        http.routes[SM] = sitemap(1, 2)
        http.routes[BASE+'2/'] = detail()
        self.assertEqual(len(a.fetch(ctx).postings), 2)
        self.assertEqual(http.requests, 5)

    def test_resume_uses_snapshot_and_prunes_closed_cache(self, _):
        http = SitemapHttp({SM: sitemap(1, 2), BASE+'1/': detail(), BASE+'2/': detail()})
        a = build_adapter('meta', CFG, http=http, page_delay=0)
        with patch('watcher.adapters.schema_sitemap.time.monotonic', side_effect=[0, 2]):
            partial = a.fetch(FetchContext(now=100, deadline=1))
        self.assertFalse(partial.complete)
        self.assertEqual(partial.cursor['ids'], ['1'])
        ctx = FetchContext(now=100, cursor=partial.cursor)
        final = a.fetch(ctx)
        self.assertEqual([p.job_id for p in final.postings], ['2'])
        self.assertEqual(len(ctx.cache['details']), 2)
        http.routes[SM] = sitemap(2)
        nxt = FetchContext(now=101, cache=ctx.cache)
        self.assertEqual([p.job_id for p in a.fetch(nxt).postings], ['2'])
        self.assertNotIn(BASE+'1/', nxt.cache['details'])

    def test_throttle_preserves_progress_and_never_completes(self, _):
        http = SitemapHttp({SM: sitemap(1, 2), BASE+'1/': detail(),
                            BASE+'2/': HttpStatusError(BASE+'2/', 429)})
        a = build_adapter('meta', CFG, http=http, page_delay=0)
        result = a.fetch(FetchContext(now=100))
        self.assertFalse(result.complete)
        self.assertEqual(result.throttled.code, 429)
        self.assertEqual(result.cursor['offset'], 1)
        self.assertEqual(result.cursor['ids'], ['1'])

    def test_missing_details_wrong_company_and_empty_sitemap_fail(self, _):
        a = build_adapter('meta', CFG, http=SitemapHttp({SM: sitemap()}), page_delay=0)
        with self.assertRaises(AdapterError):
            a.fetch()
        for page in ('<html>Sign in</html>', detail('Unrelated Company')):
            with self.assertRaises(AdapterError):
                a.parse_detail(page, BASE+'1/', '1')

    def test_country_restriction_does_not_match_global_remote_filter(self, _):
        from watcher.filters import filter_for
        a = build_adapter('meta', CFG, page_delay=0)
        p = a.parse_detail(detail(locations=[{'address': {'addressLocality': 'Remote', 'addressCountry': 'US'}}]), BASE+'1/', '1')
        # Meta config intentionally uses country/city terms, not generic remote.
        flt = filter_for({'location': ['india', 'bengaluru', 'bangalore']},
                         {'title': ['software engineer']})
        self.assertFalse(flt.matches(p))


if __name__ == '__main__':
    unittest.main()
