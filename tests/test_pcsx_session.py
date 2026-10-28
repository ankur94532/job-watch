import http.cookiejar
import os
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters import AdapterError, build_adapter
from watcher.adapters.pcsx_session import PCSXSession
from watcher.http import HttpStatusError
from watcher.robots import Robots


class Transport:
    def __init__(self, markup='<meta name="_csrf" content="initial">', error=None):
        self.markup, self.error = markup, error
        self.calls = []

    def get_text(self, url):
        self.calls.append((url, None))
        return self.markup

    def request(self, method, url, headers):
        self.calls.append((url, dict(headers)))
        if self.error:
            raise self.error
        return b'{"data":{"count":0,"positions":[]}}', {"X-CSRF-Token": "refreshed"}


class PCSXProtocol(unittest.TestCase):
    def session(self, transport):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(PCSXSession, "_make_http", return_value=transport):
            return PCSXSession("jobs.example", FakeHttp(), directory=directory.name)

    @patch("watcher.adapters.pcsx_session.fetch_robots", return_value=Robots(""))
    def test_uses_public_csrf_and_updates_it_without_exporting_context(self, _):
        transport = Transport()
        session = self.session(transport)
        session.get_json("https://jobs.example/api/pcsx/search?start=0")
        session.get_json("https://jobs.example/api/pcsx/search?start=10")
        self.assertEqual(len(transport.calls), 3)  # one shell, two searches
        self.assertEqual(transport.calls[1][1]["X-CSRF-Token"], "initial")
        self.assertEqual(transport.calls[2][1]["X-CSRF-Token"], "refreshed")
        self.assertGreater(float(transport.calls[1][1]["X-Browser-Request-Time"]), 0)
        self.assertEqual(transport.calls[1][1]["Referer"], session.page)
        self.assertNotIn("User-Agent", transport.calls[1][1])  # HttpClient keeps its honest default
        self.assertEqual(stat.S_IMODE(os.stat(session.path).st_mode), 0o600)
        with open(session.path) as f:
            self.assertNotIn("initial", f.read())  # CSRF nonce stays in memory

    @patch("watcher.adapters.pcsx_session.fetch_robots", return_value=Robots(""))
    def test_missing_context_refuses_to_issue_api_request(self, _):
        transport = Transport(markup="<html>changed layout</html>")
        with self.assertRaisesRegex(AdapterError, "no CSRF context"):
            self.session(transport).get_json("https://jobs.example/api/pcsx/search")
        self.assertEqual(len(transport.calls), 1)

    @patch("watcher.adapters.pcsx_session.fetch_robots",
           return_value=Robots("User-agent: *\nDisallow: /api/pcsx"))
    def test_robots_denial_makes_no_frontend_or_search_request(self, _):
        transport = Transport()
        with self.assertRaisesRegex(AdapterError, "robots.txt"):
            self.session(transport).get_json("https://jobs.example/api/pcsx/search")
        self.assertEqual(transport.calls, [])

    @patch("watcher.adapters.pcsx_session.fetch_robots", return_value=Robots(""))
    def test_rate_limit_propagates_once_without_refreshing_identity(self, _):
        transport = Transport(error=HttpStatusError("https://jobs.example/api/pcsx/search", 429))
        session = self.session(transport)
        with self.assertRaises(HttpStatusError):
            session.get_json("https://jobs.example/api/pcsx/search")
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(os.path.isfile(session.path))

    def test_frontend_url_uses_the_current_locale_and_default_page_size(self):
        a = build_adapter("example", {"ats": "eightfold", "host": "jobs.example",
                                     "domain": "example.com", "frontend_session": True})
        self.assertNotIn("num=", a.url("India", 10))
        self.assertIn("hl=en", a.url("India", 10))

    def test_adapter_reuses_one_session_across_all_pages(self):
        from urllib.parse import parse_qs, urlsplit

        def page(method, url, payload, cache):
            start = int(parse_qs(urlsplit(url).query)["start"][0])
            rows = [{"id": 1000 + i, "name": "Software Engineer", "locations": ["India"]}
                    for i in range(start, min(start + 10, 11))]
            return {"data": {"count": 11, "positions": rows}}

        http = FakeHttp({lambda m, u, p: "/api/pcsx/" in u: page})
        session = SimpleNamespace(http=http, get_json=http.get_json)
        a = build_adapter("example", {"ats": "eightfold", "host": "jobs.example",
                                     "domain": "example.com", "frontend_session": True},
                          http=http, page_delay=0)
        with patch("watcher.adapters.pcsx_session.PCSXSession", return_value=session) as factory:
            result = a.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.postings), 11)
        self.assertEqual(a.run_requests, 2)
        factory.assert_called_once_with("jobs.example", http)

    @patch("watcher.adapters.pcsx_session.fetch_robots", return_value=Robots(""))
    def test_server_cookie_survives_reopen_after_throttle(self, _):
        transport = Transport(error=HttpStatusError("https://jobs.example/api/pcsx/search", 429))
        session = self.session(transport)
        session.jar.set_cookie(http.cookiejar.Cookie(
            version=0, name="anonymous", value="stable", port=None, port_specified=False,
            domain="jobs.example", domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))
        with self.assertRaises(HttpStatusError):
            session.get_json("https://jobs.example/api/pcsx/search")
        with patch.object(PCSXSession, "_make_http", return_value=Transport()):
            reopened = PCSXSession("jobs.example", FakeHttp(), directory=os.path.dirname(session.path))
        self.assertEqual([(c.name, c.value) for c in reopened.jar], [("anonymous", "stable")])

    def test_session_context_is_never_sent_to_another_host_or_login(self):
        transport = Transport()
        session = self.session(transport)
        for url in ("https://other.example/api/pcsx/search", "https://jobs.example/login"):
            with self.subTest(url=url), self.assertRaises(AdapterError):
                session.get_json(url)
        self.assertEqual(transport.calls, [])
