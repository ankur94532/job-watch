import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tests.fakes import FakeHttp
from watcher import yamlish
from watcher.detect import Candidate, Detector, find_candidates, parse_query
from watcher.filters import Filter
from watcher.http import HttpClient, HttpStatusError, NetworkError, parse_retry_after, user_agent
from watcher.lock import AlreadyRunning, RunLock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Filters(unittest.TestCase):
    f = Filter(title=["software engineer", "backend", "mts", "back end"], location=["india", "bengaluru", "remote"])

    def test_word_boundaries_and_punctuation(self):
        self.assertTrue(self.f.title_ok("Software Engineering - MTS"))
        self.assertTrue(self.f.title_ok("Back-End Developer"))
        self.assertFalse(self.f.title_ok("Distributed Systems Lead"))   # "mts" inside "systems"
        self.assertTrue(self.f.title_ok("SOFTWARE ENGINEER"))

    def test_no_seniority_filtering(self):
        for t in ("Software Engineer", "Senior Software Engineer", "Software Engineer II", "Principal Backend Engineer"):
            self.assertTrue(self.f.title_ok(t), t)

    def test_location(self):
        self.assertTrue(self.f.location_ok("Bengaluru"))
        self.assertTrue(self.f.location_ok("India - Hyderabad"))
        self.assertFalse(self.f.location_ok("London"))


class Yaml(unittest.TestCase):
    def test_parses_shipped_config(self):
        with open(os.path.join(ROOT, "config.yaml")) as fh:
            text = fh.read()
        cfg = yamlish.loads(text)
        self.assertIn("software engineer", cfg["defaults"]["title"])
        self.assertEqual(cfg["companies"]["salesforce"]["sites"], ["External_Career_Site"])
        try:
            import yaml
        except ImportError:
            return
        self.assertEqual(cfg, yaml.safe_load(text))

    def test_subset(self):
        doc = textwrap.dedent("""
            # comment
            a:
              b: 1
              c: "x # not a comment"
              d: [one, 'two, three',
                  four]   # trailing
              e: {}
              f:
                - x
                - y
            g: true
        """)
        self.assertEqual(yamlish.loads(doc), {
            "a": {"b": 1, "c": "x # not a comment", "d": ["one", "two, three", "four"], "e": {}, "f": ["x", "y"]},
            "g": True,
        })

    def test_rejects_what_it_cannot_read(self):
        with self.assertRaises(yamlish.ConfigError):
            yamlish.loads("a: &anchor 1\n")
        with self.assertRaises(yamlish.ConfigError):
            yamlish.loads("a:\n  - b: 1\n")


class ShippedConfig(unittest.TestCase):
    """The config is data, and data rots. Catch a bad edit before launchd does."""

    def setUp(self):
        from watcher import config
        self.config = config
        self.cfg = config.load(os.path.join(ROOT, "config.yaml"))

    def test_every_active_company_builds_an_adapter(self):
        from watcher.adapters import build_adapter
        active, skipped = self.config.active_companies(self.cfg)
        self.assertGreater(len(active), 20)
        for name, c in active:
            build_adapter(name, c)                     # raises on a missing token/tenant/site
        for name, note in skipped:
            self.assertTrue(note, f"{name} is skipped with no note saying why")

    def test_workday_entries_have_a_plausible_shard(self):
        import re
        for name, c in self.cfg["companies"].items():
            if c.get("ats") == "workday":
                self.assertRegex(c["shard"], r"^wd\d+$", name)
                self.assertIsInstance(c["sites"], list, name)

    def test_filters_match_the_kind_of_title_this_is_for(self):
        from watcher.filters import filter_for
        f = filter_for({}, self.cfg["defaults"])
        for title in ("Software Engineer", "Software Engineering - MTS", "Backend Engineer",
                      "Member of Technical Staff", "SDE II", "Distributed Systems Engineer"):
            self.assertTrue(f.title_ok(title), title)
        for title in ("Account Executive", "Recruiter", "Pharmacy Manager"):
            self.assertFalse(f.title_ok(title), title)
        for loc in ("Bengaluru", "India - Hyderabad", "Gurugram", "Chennai", "Remote"):
            self.assertTrue(f.location_ok(loc), loc)
        self.assertFalse(f.location_ok("London"))


class Lock(unittest.TestCase):
    def test_exclusive_and_released_when_holder_is_killed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "jobs.db.lock")
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys,time; sys.path.insert(0, sys.argv[2]); from watcher.lock import RunLock; "
                 "l = RunLock(sys.argv[1]).__enter__(); print('held', flush=True); time.sleep(60)",
                 path, ROOT],
                stdout=subprocess.PIPE,
            )
            self.assertEqual(child.stdout.readline().strip(), b"held")
            with self.assertRaises(AlreadyRunning):
                with RunLock(path):
                    pass
            os.kill(child.pid, signal.SIGKILL)       # the "lid closed mid-run" case
            child.wait()
            child.stdout.close()
            self.assertTrue(os.path.exists(path))    # file left behind...
            with RunLock(path):                      # ...but it is not a lock
                pass


class Http(unittest.TestCase):
    def test_honest_user_agent(self):
        os.environ["JOBSWATCH_CONTACT_URL"] = "https://github.com/me/job-watch"
        try:
            ua = user_agent()
        finally:
            del os.environ["JOBSWATCH_CONTACT_URL"]
        self.assertTrue(ua.startswith("job-watch/"))
        self.assertIn("https://github.com/me/job-watch", ua)
        self.assertNotIn("Mozilla", ua)

    def test_retry_after(self):
        self.assertEqual(parse_retry_after("120"), 120)
        self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 60), 60)
        self.assertIsNone(parse_retry_after(None))

    def test_4xx_not_retried_5xx_retried(self):
        import urllib.error

        calls = []

        def opener(code):
            def f(req, timeout):
                calls.append(code)
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            return f

        c = HttpClient(retries=2, sleep=lambda s: None, opener=opener(429), online=lambda: True)
        with self.assertRaises(HttpStatusError):
            c.request("GET", "https://x")
        self.assertEqual(calls, [429])
        calls.clear()
        c = HttpClient(retries=2, sleep=lambda s: None, opener=opener(503), online=lambda: True)
        with self.assertRaises(HttpStatusError):
            c.request("GET", "https://x")
        self.assertEqual(calls, [503, 503, 503])

    def test_html_instead_of_json_is_a_clean_failure(self):
        """Workday tenants redirect to a maintenance page; that must not be a traceback."""
        import io

        from watcher.http import InvalidResponse

        class Resp(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        c = HttpClient(opener=lambda req, timeout: Resp(b"<!DOCTYPE html><html>maintenance"),
                       online=lambda: True)
        with self.assertRaises(InvalidResponse) as caught:
            c.get_json("https://x.myworkdayjobs.com/wday/cxs/x/y/jobs")
        self.assertIn("expected JSON", str(caught.exception))

    def test_offline_does_not_retry(self):
        import urllib.error

        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise urllib.error.URLError("nodename nor servname provided")

        c = HttpClient(retries=3, sleep=lambda s: None, opener=opener, online=lambda: False)
        with self.assertRaises(NetworkError):
            c.request("GET", "https://x")
        self.assertEqual(len(calls), 1)


class Detect(unittest.TestCase):
    def test_parse_ats_urls(self):
        c = find_candidates(
            "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/India---Bangalore/X_JR1", "u")
        self.assertEqual((c[0].ats, c[0].cfg), ("workday", {"tenant": "salesforce", "shard": "wd12",
                                                            "sites": ["External_Career_Site"]}))
        html = '<a href="https://job-boards.greenhouse.io/gleanwork/jobs/1">x</a> <script src="https://boards.greenhouse.io/embed/job_board/js?for=rubrik"></script>'
        toks = [(x.ats, x.cfg["token"]) for x in find_candidates(html, "h")]
        self.assertIn(("greenhouse", "gleanwork"), toks)
        self.assertIn(("greenhouse", "rubrik"), toks)
        self.assertEqual(find_candidates("https://jobs.lever.co/meesho/abc", "u")[0].cfg, {"token": "meesho"})

    def test_parse_query(self):
        self.assertEqual(parse_query("Tower Research"), ("towerresearch", None, None))
        self.assertEqual(parse_query("rubrik.com")[:2], ("rubrik", "rubrik.com"))
        self.assertEqual(parse_query("qdrant.tech")[:2], ("qdrant", "qdrant.tech"))     # any TLD
        self.assertEqual(parse_query("media.net")[:2], ("media", "media.net"))
        self.assertEqual(parse_query("Tower Research Capital")[:2], ("towerresearchcapital", None))
        key, domain, url = parse_query("https://rbs.wd3.myworkdayjobs.com/RBS/job/London/X_R-1")
        self.assertEqual((key, domain), ("rbs", None))

    def test_smartrecruiters_empty_200_is_a_miss(self):
        url = "https://api.smartrecruiters.com/v1/companies/nope/postings?limit=100&offset=0"
        http = FakeHttp({url: {"offset": 0, "limit": 100, "totalFound": 0, "content": []}})
        det = Detector(http=http, log=lambda m: None)
        self.assertIsNone(det.verify("nope", Candidate("smartrecruiters", {"token": "nope"}, "guess", guessed=True)))

    def test_stale_guess_is_flagged(self):
        import datetime as dt

        url = "https://api.smartrecruiters.com/v1/companies/Zomato1/postings?limit=100&offset=0"
        body = {"offset": 0, "limit": 100, "totalFound": 1, "content": [{
            "id": "1", "name": "Software Engineer – Back End", "releasedDate": "2016-12-20T06:54:25.000Z",
            "company": {"name": "Zomato"}, "location": {"fullLocation": "Gurugram, HR, India"}}]}
        det = Detector(http=FakeHttp({url: body}), log=lambda m: None, today=dt.date(2026, 9, 16))
        d = det.verify("zomato", Candidate("smartrecruiters", {"token": "Zomato1"}, "guess", guessed=True))
        self.assertTrue(any("stale" in w for w in d.warnings))
        self.assertIn("# WARNING: stale", d.yaml_block())


if __name__ == "__main__":
    unittest.main()
