import unittest
from unittest.mock import patch

from tests.fakes import FakeHttp
from watcher.adapters.arm import ArmAdapter
from watcher.adapters.base import AdapterError, FetchContext
from watcher.adapters.public_html import CountedHTML


def card(job_id="100", title="Software Engineer", city="bengaluru",
         location="Bengaluru, India"):
    return f'''<li class="job-card fs-start">
      <a class="job-card__title fs-11" href="/job/{city}/software-engineer/33099/{job_id}"
         data-job-id="{job_id}">{title}</a>
      <span class="location">{location}</span>
      <button data-job-id="{job_id}" data-org-id="33099">Save</button>
    </li>'''


def page(cards, total=None, current=1, nxt=None, **changes):
    total = len(cards) if total is None else total
    pages = (total + 14) // 15
    attrs = {
        "data-total-results": str(total), "data-total-job-results": str(total),
        "data-total-pages": str(pages), "data-current-page": str(current),
        "data-records-per-page": "15", "data-facet-term": "1269750",
        "data-facet-type": "2", "data-organization-ids": "33099",
    }
    attrs.update({key.replace("_", "-"): str(value) for key, value in changes.items()})
    state = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    next_link = f'<a class="next" href="{nxt}">Next</a>' if nxt else ""
    return (f'<html><head><title>Arm Job Search Results in India</title>'
            f'<meta name="site-organization-id" content="33099"></head><body>'
            f'<section id="search-results" {state}></section>'
            f'<ul class="list-reset" id="search-results-jobs" data-results-count="{total}">'
            f'{"".join(cards)}</ul>{next_link}</body></html>')


@patch.object(CountedHTML, "check_robots")
class ArmSafety(unittest.TestCase):
    def adapter(self):
        return ArmAdapter("arm", {"host": "careers.arm.com", "organization": "Arm"},
                          page_delay=0)

    def test_complete_counted_pages_and_foreign_primary_location(self, _):
        adapter = self.adapter()
        first_url = "https://careers.arm.com" + adapter.path
        second_url = ("https://careers.arm.com/location/india-jobs/"
                      "33099/1269750/2/2")
        first_cards = [card(str(i)) for i in range(100, 115)]
        second_cards = [card("115", title="Technical Director", city="cambridge",
                             location="Cambridge, United Kingdom")]
        adapter.http = FakeHttp({
            first_url: page(first_cards, total=16, nxt=second_url),
            second_url: page(second_cards, total=16, current=2),
        })
        result = adapter.fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.declared, 16)
        self.assertEqual(len(result.postings), 16)
        self.assertEqual(result.postings[-1].location, "Cambridge, United Kingdom | India")
        self.assertEqual(result.postings[-1].url,
                         "https://careers.arm.com/job/cambridge/software-engineer/33099/115")

    def test_resume_preserves_exact_count_and_ids(self, _):
        adapter = self.adapter()
        first_url = "https://careers.arm.com" + adapter.path
        second_path = "/location/india-jobs/33099/1269750/2/2"
        adapter.http = FakeHttp({
            first_url: page([card(str(i)) for i in range(15)], total=16, nxt=second_path),
            "https://careers.arm.com" + second_path: page([card("15")], total=16, current=2),
        })
        first = adapter.fetch(FetchContext(deadline=0))
        self.assertFalse(first.complete)
        self.assertEqual(len(first.cursor["ids"]), 15)
        final = adapter.fetch(FetchContext(cursor=first.cursor))
        self.assertTrue(final.complete)
        self.assertEqual([p.job_id for p in final.postings], ["15"])

    def test_count_drift_or_overlap_restarts(self, _):
        adapter = self.adapter()
        first_url = "https://careers.arm.com" + adapter.path
        next_path = "/location/india-jobs/33099/1269750/2/2"
        adapter.http = FakeHttp({
            first_url: page([card(str(i)) for i in range(15)], total=16, nxt=next_path),
            "https://careers.arm.com" + next_path: page([card("0")], total=17, current=2),
        })
        result = adapter.fetch()
        self.assertTrue(result.restart)
        self.assertEqual(result.postings, [])

    def test_identity_search_state_and_cards_fail_closed(self, _):
        good = page([card()])
        bad = [
            good.replace("Arm Job Search Results in India", "Other jobs"),
            good.replace('content="33099"', 'content="7"', 1),
            good.replace('data-facet-term="1269750"', 'data-facet-term="7"'),
            good.replace('data-total-job-results="1"', 'data-total-job-results="2"'),
            good.replace('data-org-id="33099"', 'data-org-id="7"'),
            good.replace('data-job-id="100">Software', 'data-job-id="999">Software', 1),
            good.replace('<span class="location">Bengaluru, India</span>', ''),
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(AdapterError):
                self.adapter().parse(body)

    def test_missing_or_ambiguous_next_link_fails(self, _):
        cards = [card(str(i)) for i in range(15)]
        with self.assertRaises(AdapterError):
            self.adapter().parse(page(cards, total=16))
        ambiguous = page(cards, total=16, nxt="/page/2").replace(
            "</body>", '<a class="next" href="/different">Next</a></body>')
        with self.assertRaises(AdapterError):
            self.adapter().parse(ambiguous)


if __name__ == "__main__":
    unittest.main()
