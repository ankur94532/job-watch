"""Arm's server-rendered, country-filtered TalentBrew search pages."""

import html
import re

from .base import AdapterError, Posting, join_locations
from .html import text
from .public_html import CountedHTML


INDIA_FACET = "1269750"
ORGANIZATION_ID = "33099"


class ArmAdapter(CountedHTML):
    ats = "arm"
    path = f"/location/india-jobs/{ORGANIZATION_ID}/{INDIA_FACET}/2/1"

    def parse(self, page):
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
        org = re.search(r'<meta\s+name="site-organization-id"\s+content="([^"]+)"', page)
        search = re.search(r'<section\s+id="search-results"\s+([^>]*)>', page)
        if (not title or text(title.group(1)) != "Arm Job Search Results in India"
                or not org or org.group(1) != ORGANIZATION_ID or not search):
            raise AdapterError(f"{self.company}: wrong Arm search identity")
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', search.group(1)))
        expected = {
            "data-facet-term": INDIA_FACET,
            "data-facet-type": "2",
            "data-organization-ids": ORGANIZATION_ID,
            "data-records-per-page": "15",
        }
        if any(attrs.get(key) != value for key, value in expected.items()):
            raise AdapterError(f"{self.company}: mismatched Arm India search state")
        try:
            total = int(attrs["data-total-job-results"])
            current = int(attrs["data-current-page"])
            pages = int(attrs["data-total-pages"])
            per_page = int(attrs["data-records-per-page"])
        except (KeyError, ValueError) as exc:
            raise AdapterError(f"{self.company}: invalid Arm pagination state") from exc
        if (total < 1 or current < 1 or pages != (total + per_page - 1) // per_page
                or attrs.get("data-total-results") != str(total)):
            raise AdapterError(f"{self.company}: inconsistent Arm result count")

        listing = re.search(
            r'<ul\s+class="[^"]*"\s+id="search-results-jobs"\s+'
            r'data-results-count="(\d+)"[^>]*>(.*?)</ul>', page, re.S)
        if not listing or int(listing.group(1)) != total:
            raise AdapterError(f"{self.company}: missing Arm results list/count")
        cards = re.findall(r'<li\s+class="[^"]*job-card[^"]*"[^>]*>(.*?)</li>',
                           listing.group(2), re.S)
        out = []
        for card in cards:
            link = re.search(
                rf'<a\s+class="[^"]*job-card__title[^"]*"\s+'
                rf'href="(/job/([^/]+)/([^/]+)/{ORGANIZATION_ID}/(\d+))"\s+'
                r'data-job-id="(\d+)"[^>]*>(.*?)</a>', card, re.S)
            location = re.search(r'<span\s+class="location"[^>]*>(.*?)</span>', card, re.S)
            button = re.search(
                r'<button[^>]*data-job-id="(\d+)"[^>]*data-org-id="(\d+)"', card)
            if (not link or not location or not button or link.group(4) != link.group(5)
                    or button.group(1) != link.group(4)
                    or button.group(2) != ORGANIZATION_ID):
                raise AdapterError(f"{self.company}: malformed Arm job card")
            job_title = text(link.group(6))
            primary = text(location.group(1))
            if not job_title or not primary:
                raise AdapterError(f"{self.company}: missing Arm title/location")
            # The exact country facet establishes India membership even when a
            # multi-location job's card/URL displays only its first foreign office.
            place = join_locations(primary, "India")
            job_id = link.group(4)
            out.append(Posting(
                company=self.company,
                ats=self.ats,
                job_id=job_id,
                title=job_title,
                location=place,
                remote="remote" in place.lower(),
                posted_at="",
                url="https://" + self.cfg["host"] + html.unescape(link.group(1)),
                raw={"primary_location": primary, "india_facet": INDIA_FACET},
            ))

        first = (current - 1) * per_page + 1
        last = first + len(out) - 1
        next_links = set(html.unescape(url) for url in re.findall(
            r'<a\s+class="next"[^>]*href="([^"]+)"', page))
        if len(next_links) > 1:
            raise AdapterError(f"{self.company}: ambiguous Arm next page")
        next_url = next(iter(next_links), None)
        if bool(next_url) != (current < pages):
            raise AdapterError(f"{self.company}: missing/inconsistent Arm next page")
        return out, total, first, last, next_url
