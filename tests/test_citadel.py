import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from watcher.adapters.base import AdapterError
from watcher.adapters.citadel import CitadelSecuritiesAdapter, BASE
from watcher.filters import Filter
from watcher.http import HttpStatusError


def card(slug="software-engineer", location="Gurugram, London"):
    return (f'<a class="careers-listing-card js-career-card" href="{BASE}/careers/details/{slug}/">'
            '<div><h2>Software Engineer &amp; Researcher</h2></div>'
            f'<div class="careers-listing-card__location">{location}</div></a>')


def payload(content=None, **changes):
    return dict({"number_of_post": 1, "found_posts": 1, "post_per_page": 100,
                 "page_number": 1, "content": card() if content is None else content}, **changes)


class CitadelSafety(unittest.TestCase):
    def setUp(self):
        self.a = CitadelSecuritiesAdapter("citadelsecurities", {})

    def test_complete_locations_and_slug_is_not_a_requisition(self):
        result = self.a.parse(payload())
        p = result.postings[0]
        self.assertTrue(result.complete)
        self.assertEqual(result.declared, 1)
        self.assertTrue(Filter(location=["gurugram"]).matches(p))
        self.assertEqual(p.title, "Software Engineer & Researcher")
        self.assertEqual(p.job_id, "software-engineer")
        self.assertEqual((p.req_id, p.posted_at), ("", ""))

    def test_truncated_unexpected_or_duplicate_inventory_rejected(self):
        for data in ({}, [], payload(found_posts=True), payload(page_number=2),
                     payload(post_per_page=10), payload(found_posts=2),
                     payload(found_posts=101, number_of_post=101),
                     payload(card()+card(), found_posts=2, number_of_post=2),
                     payload(""), payload(card(location="")),
                     payload(card().replace(BASE, "https://unrelated.test")),
                     payload(card().replace("<h2>", "<h3>"))):
            with self.subTest(data=data), self.assertRaises(AdapterError):
                self.a.parse(data)

    def test_explicit_empty(self):
        result = self.a.parse(payload("", number_of_post=0, found_posts=0))
        self.assertTrue(result.complete)
        self.assertEqual(result.postings, [])
        self.assertEqual(result.empty_evidence, "found_posts=0")

    @patch("watcher.adapters.citadel.time.sleep")
    @patch.object(CitadelSecuritiesAdapter, "check_robots")
    def test_public_form_request_and_throttle(self, robots, sleep):
        class HTTP:
            def request(self, method, url, data, headers):
                self.sent = method, parse_qs(data.decode()), headers
                return json.dumps(payload()).encode(), {}
        self.a.http = HTTP()
        self.assertTrue(self.a.fetch().complete)
        self.assertEqual(self.a.http.sent[0], "POST")
        self.assertEqual(self.a.http.sent[1]["action"], ["careers_listing_filter"])
        self.assertEqual(self.a.http.sent[1]["per_page"], ["100"])
        sleep.assert_called_once_with(10)
        robots.assert_called_once()
        with patch.object(self.a.http, "request", side_effect=HttpStatusError("url", 403)):
            with self.assertRaises(HttpStatusError):
                self.a.fetch()


if __name__ == "__main__":
    unittest.main()
