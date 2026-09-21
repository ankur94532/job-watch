import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, FetchContext, build_adapter
from watcher.http import HttpStatusError
from watcher.adapters.smartrecruiters_html import SmartRecruitersHTMLAdapter


CFG = {"ats": "smartrecruiters_html", "token": "LinkedIn3",
       "organization": "LinkedIn", "country": "India"}


def card(key):
    return (f'<li><a href="https://jobs.smartrecruiters.com/LinkedIn3/{key}-software-engineer" '
            f'class="details js-job-ad-link" aria-label="Software Engineer - REQ{key}">'
            '<h4 class="job-title">Software Engineer</h4></a></li>')


def index(total=12, keys=None, location="Bengaluru, India"):
    keys = list(range(1, min(9, total) + 1)) if keys is None else keys
    more = ('<a class="js-more" data-page="0" data-value="Bengaluru%2C%20IN" '
            'data-type="location" data-more="/LinkedIn3/api/more">Show more jobs</a>') if total > 9 else ''
    return ('<title>Careers at LinkedIn</title><section id="st-openings">'
            '<div class="js-openings-load" data-page="6" data-groups-pages="6">'
            f'<section class="js-group" data-qty="{total}"><h3>{location}</h3><span class="title">{total} jobs</span>'
            + ''.join(map(card, keys)) + more + '</section></div></section>')


def make(total=12, final=None, more=None, cfg=None):
    a = build_adapter('linkedin', dict(CFG, **(cfg or {})), page_delay=0)
    heads = iter([index(total), final or index(total)])
    a.http = FakeHttp({a.url(): lambda *args: next(heads),
                      a.more_url({'value': 'Bengaluru, IN'}, 1):
                      more if more is not None else ''.join(card(i) for i in range(10, total + 1))})
    return a


@patch.object(SmartRecruitersHTMLAdapter, 'check_robots')
class SmartRecruitersHTMLTests(unittest.TestCase):
    def test_complete_counted_sweep_and_requisition(self, _):
        a = make()
        r = a.fetch()
        self.assertTrue(r.complete)
        self.assertEqual(r.declared, 12)
        self.assertEqual(len(r.postings), 12)
        self.assertEqual(r.postings[0].req_id, 'REQ1')
        self.assertEqual(r.postings[0].location, 'Bengaluru, India')
        self.assertEqual(len(a.http.calls), 3)

    def test_resume_holds_rows_until_final_verification(self, _):
        a = make(cfg={'max_requests_per_run': 1})
        r = a.fetch()
        self.assertFalse(r.complete)
        self.assertEqual(r.postings, [])
        second = a.fetch(FetchContext(cursor=r.cursor))
        self.assertFalse(second.complete)
        self.assertEqual(second.postings, [])
        final = a.fetch(FetchContext(cursor=second.cursor))
        self.assertTrue(final.complete)
        self.assertEqual(len(final.postings), 12)
        self.assertEqual(len(a.http.calls), 3)

    def test_changed_count_or_head_restarts_without_rows(self, _):
        for final in (index(13), index(keys=[99] + list(range(2, 10)))):
            with self.subTest(final=final):
                r = make(final=final).fetch()
                self.assertTrue(r.restart)
                self.assertFalse(r.complete)
                self.assertEqual(r.postings, [])

    def test_page_overlap_restarts(self, _):
        r = make(more=card(9) + card(10) + card(11)).fetch()
        self.assertTrue(r.restart)
        self.assertEqual(r.postings, [])

    def test_missing_or_duplicate_page_cannot_complete(self, _):
        for more in ('', card(10), card(10) + card(10) + card(11)):
            with self.subTest(more=more), self.assertRaises(AdapterError):
                make(more=more).fetch()

    def test_throttle_retains_exact_pending_page(self, _):
        error = HttpStatusError('https://careers.smartrecruiters.com', 429, retry_after=600)
        a = make(more=error)
        first = a.fetch()
        self.assertIs(first.throttled, error)
        self.assertEqual(first.cursor['page'], 1)
        self.assertEqual(len(first.cursor['groups'][0]['rows']), 9)
        self.assertEqual(first.postings, [])
        a.http.routes[a.more_url({'value': 'Bengaluru, IN'}, 1)] = ''.join(card(i) for i in range(10, 13))
        self.assertEqual(len(a.fetch(FetchContext(cursor=first.cursor)).postings), 12)

    def test_shape_identity_scope_and_link_validation(self, _):
        a = make()
        bad = [index().replace('Careers at LinkedIn', 'Careers at Other'),
               index().replace('data-page="6"', 'data-page="0"'),
               index(keys=[1]), index(location='London, United Kingdom'),
               index().replace('https://jobs.smartrecruiters.com', 'https://example.com'),
               index().replace('/LinkedIn3/api/more', 'https://example.com/more'),
               index().replace('<h4 class="job-title">Software Engineer</h4>', '<h4></h4>'),
               index().replace('data-qty="12"', 'data-count="12"'),
               index().replace('12 jobs', '11 jobs')]
        for page in bad:
            with self.subTest(page=page), self.assertRaises(AdapterError):
                a.index(page)

    def test_explicit_empty_is_verified_twice(self, _):
        a = make()
        a.http.routes[a.url()] = ('<title>Careers at LinkedIn</title>'
                                 '<p id="st-noOpeningsFound">No job postings are currently available.</p>')
        r = a.fetch()
        self.assertTrue(r.complete)
        self.assertEqual(r.declared, 0)
        self.assertEqual(r.empty_evidence, a.EMPTY)
        self.assertEqual(len(a.http.calls), 2)

    def test_legacy_or_changed_scope_cursor_restarts_without_http(self, _):
        a = make()
        for cur in ({'offset': 10}, {'v': 1, 'scope': ['wrong']}):
            self.assertTrue(a.fetch(FetchContext(cursor=cur)).restart)
        self.assertEqual(a.http.calls, [])

    def test_global_duplicate_ids_cannot_close_jobs(self, _):
        a = make(total=1)
        page = index(1)
        group = ('<section class="js-group" data-qty="1"><h3>Mumbai, India</h3>'
                 '<span class="title">1 job</span>' + card(1) + '</section>')
        page = page.replace('</div></section>', group + '</div></section>')
        a.http.routes[a.url()] = page
        with self.assertRaisesRegex(AdapterError, 'duplicate'):
            a.fetch()
