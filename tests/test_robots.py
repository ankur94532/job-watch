"""robots.txt matching, against the two real files that drove the decisions.

urllib.robotparser gets both of these wrong by dropping query strings, which is
how "Google disallows its own results page 1" appears out of thin air.
"""

import os
import sys
import unittest

from tests.fakes import FIXTURES
from watcher.robots import Robots

UA = "job-watch/0.1 (+https://github.com/x/job-watch; personal job-alert poller)"
GOOG = "https://www.google.com/about/careers/applications/jobs/results"


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return Robots(f.read())


class Google(unittest.TestCase):
    def setUp(self):
        self.r = load("robots_google.txt")

    def test_page_one_is_allowed_but_pagination_is_not(self):
        self.assertTrue(self.r.allowed(UA, GOOG + "?location=India&sort_by=date"))
        self.assertFalse(self.r.allowed(UA, GOOG + "?location=India&page=2"))
        self.assertFalse(self.r.allowed(UA, GOOG + "/?page=3"))
        self.assertIn("page=", self.r.why(UA, GOOG + "?location=India&page=2"))

    def test_stdlib_disagreed_and_was_wrong_before_3_14(self):
        """Pinning the reason we do not use urllib.robotparser.

        The stdlib dropped the query string, so Google's
        `Disallow: /...results?page=` blocked the unpaginated results page too.
        **Python 3.14 fixed that**, so on a new enough interpreter the stdlib
        now agrees with us here. The reason to keep our own matcher stands
        anyway: this still runs on the system 3.9 (where the bug is live), and
        `robotparser` exposes no way to report WHICH rule matched, which
        `Robots.why()` provides for logs and for writing an honest config note.

        Asserted both ways round so this test keeps documenting the behaviour
        instead of failing the day the interpreter changes.
        """
        import urllib.robotparser
        with open(os.path.join(FIXTURES, "robots_google.txt")) as f:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(f.read().splitlines())
        stdlib_ok = rp.can_fetch(UA, GOOG + "?location=India&sort_by=date")
        self.assertEqual(stdlib_ok, sys.version_info >= (3, 14))
        # Ours is right on every version, which is the part that matters.
        self.assertTrue(self.r.allowed(UA, GOOG + "?location=India&sort_by=date"))
        # And the pagination rule is honoured by both.
        self.assertFalse(rp.can_fetch(UA, GOOG + "?location=India&page=2"))
        self.assertFalse(self.r.allowed(UA, GOOG + "?location=India&page=2"))

    def test_other_disallowed_careers_paths_still_blocked(self):
        self.assertFalse(self.r.allowed(
            UA, "https://www.google.com/about/careers/applications/candidate-prep"))


class TurboHire(unittest.TestCase):
    def setUp(self):
        self.r = load("robots_turbohire.txt")

    def test_everything_is_disallowed_for_us(self):
        url = "https://flipkart.turbohire.co/careerpage/4d757ba0-3d57-448a-b82c-238ed87ac90f"
        self.assertFalse(self.r.allowed(UA, url))
        self.assertEqual(self.r.why(UA, url), "Disallow: /")

    def test_googlebot_is_allowed_where_we_are_not(self):
        url = "https://flipkart.turbohire.co/careerpage/abc"
        self.assertTrue(self.r.allowed("Googlebot/2.1", url))
        self.assertFalse(self.r.allowed(UA, url))

    def test_this_client_is_named_explicitly(self):
        self.assertIn("anthropic-ai", self.r.groups)
        self.assertFalse(self.r.allowed("anthropic-ai", "https://flipkart.turbohire.co/job/1"))


class Semantics(unittest.TestCase):
    def test_longest_match_wins_and_allow_breaks_ties(self):
        r = Robots("User-agent: *\nDisallow: /jobs\nAllow: /jobs/public\n")
        self.assertFalse(r.allowed(UA, "https://x/jobs/secret"))
        self.assertTrue(r.allowed(UA, "https://x/jobs/public/1"))

    def test_wildcards_and_end_anchor(self):
        r = Robots("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(r.allowed(UA, "https://x/a/b/report.pdf"))
        self.assertTrue(r.allowed(UA, "https://x/a/report.pdf?ok=1"))

    def test_empty_disallow_allows_everything(self):
        self.assertTrue(Robots("User-agent: *\nDisallow:\n").allowed(UA, "https://x/anything"))

    def test_missing_file_allows_rather_than_inventing_a_ban(self):
        self.assertTrue(Robots("", status=404).allowed(UA, "https://x/anything"))


if __name__ == "__main__":
    unittest.main()
