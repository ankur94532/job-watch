import unittest
from unittest.mock import patch

from watcher.adapters.base import AdapterError
from watcher.adapters.mathworks import FEED, MathWorksAdapter


def item(job_id="123", title="Software Engineer", location="IN-Bangalore", country="IN",
         link=None):
    link = link or ("https://www.mathworks.com/company/jobs/opportunities/"
                    f"{job_id}-software-engineer.html")
    return f"""
    <item>
      <title>{title} ({job_id}-AB)</title><title_raw>{title}</title_raw><id>{job_id}</id>
      <link>{link}</link>
      <location><locationName>{location}</locationName><city>Bangalore</city>
        <country>{country}</country></location>
    </item>"""


def feed(*items, title="MathWorks Job Postings",
         link="https://www.mathworks.com/company/jobs/opportunities/"):
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>'
            f"<link>{link}</link><description>Jobs</description>{''.join(items)}</channel></rss>")


class XmlHttp:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, method, url, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        return self.body.encode(), {"Content-Type": "application/xml"}


@patch.object(MathWorksAdapter, "check_robots")
class MathWorksSafety(unittest.TestCase):
    def adapter(self, body, **cfg):
        adapter = MathWorksAdapter("mathworks", {"country": "IN", **cfg}, page_delay=0)
        adapter.http = XmlHttp(body)
        return adapter

    def test_filters_country_and_collapses_exact_duplicates(self, _):
        body = feed(item(), item(), item("124", location="US-MA-Natick", country="US",
                                        link=("https://www.mathworks.com/company/jobs/"
                                              "opportunities/124.html")),
                    item("125", title="Platform Engineer", location="IN-Hyderabad"))
        adapter = self.adapter(body)
        result = adapter.fetch()
        self.assertTrue(result.complete)
        self.assertEqual([p.job_id for p in result.postings], ["123", "125"])
        self.assertEqual(result.postings[0].location, "IN-Bangalore")
        self.assertEqual(result.postings[1].req_id, "125")
        self.assertEqual(result.postings[0].posted_at, "")
        self.assertEqual(adapter.http.calls[0][1], FEED)

    def test_combines_distinct_target_country_locations(self, _):
        body = feed(item(location="IN-Bangalore"), item(location="IN-Hyderabad"),
                    item(location="US-MA-Natick", country="US"))
        posting = self.adapter(body).fetch().postings[0]
        self.assertEqual(posting.location, "IN-Bangalore | IN-Hyderabad")
        self.assertEqual(posting.raw["countries"], ["IN", "IN", "US"])

    def test_feed_and_job_identity_fail_closed(self, _):
        bad = [
            "not xml",
            feed(item(), title="Other Company Jobs"),
            feed(item(), link="https://example.com/jobs/"),
            feed(),
            feed(item(job_id="abc")),
            feed(item(title="")),
            feed(item(location="")),
            feed(item(country="India")),
            feed(item(link="https://example.com/company/jobs/opportunities/123-job.html")),
            feed(item(link="https://www.mathworks.com/company/jobs/opportunities/999-job.html")),
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(AdapterError):
                self.adapter(body).fetch()

    def test_conflicting_duplicate_is_rejected(self, _):
        for duplicate in (item(title="Different title"),
                          item(link="https://www.mathworks.com/company/jobs/opportunities/123-other.html")):
            with self.subTest(duplicate=duplicate), self.assertRaises(AdapterError):
                self.adapter(feed(item(), duplicate)).fetch()

    def test_empty_target_country_fails_closed(self, _):
        with self.assertRaises(AdapterError):
            self.adapter(feed(item(country="US", location="US-MA-Natick"))).fetch()

    def test_country_config_and_feed_override(self, _):
        with self.assertRaises(AdapterError):
            self.adapter(feed(item()), country="India").fetch()
        custom = "https://www.mathworks.com/custom-feed.xml"
        adapter = self.adapter(feed(item()), feed=custom)
        adapter.fetch()
        self.assertEqual(adapter.http.calls[0][1], custom)


if __name__ == "__main__":
    unittest.main()
