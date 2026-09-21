"""Citadel Securities' public WordPress careers search form.

The form returns counted JSON containing HTML cards. Request up to 100 in one
response; fail closed if the inventory grows beyond that bound. Public URL
slugs identify postings; they are not employer requisition numbers. Dates on
detail pages appear regenerated and are deliberately omitted.
"""

import html
import json
import re
import time
from urllib.parse import urlencode

from .base import AdapterError, FetchResult, Posting
from .embedded import RobotsAware
from .html import text

BASE = "https://www.citadelsecurities.com"
URL = BASE + "/wp-admin/admin-ajax.php"


class CitadelSecuritiesAdapter(RobotsAware):
    ats = "citadel_securities"
    html = True

    def fetch(self, ctx=None):
        if ctx and ctx.cursor:
            raise AdapterError(f"{self.company}: unexpected Citadel cursor")
        self.check_robots(URL)
        # Its published robots file asks for a ten-second crawl delay.
        time.sleep(10)
        body, _ = self.http.request("POST", URL, data=urlencode({
            "action": "careers_listing_filter", "current_page": 1, "per_page": 100,
            "sort_order": "DESC", "jobTitle": "",
            "selected-job-sections": "323,325,324,326",
        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as e:
            raise AdapterError(f"{self.company}: invalid Citadel JSON") from e
        return self.parse(payload)

    def parse(self, payload):
        if (not isinstance(payload, dict)
                or any(type(payload.get(k)) is not int for k in
                       ("number_of_post", "found_posts", "post_per_page", "page_number"))
                or payload["page_number"] != 1 or payload["post_per_page"] != 100
                or not 0 <= payload["found_posts"] <= 100
                or payload["number_of_post"] != payload["found_posts"]
                or not isinstance(payload.get("content"), str)):
            raise AdapterError(f"{self.company}: incomplete/invalid Citadel inventory")
        cards = re.findall(r'<a\b([^>]*class="careers-listing-card[^>]*?)>(.*?)</a>',
                           payload["content"], re.S)
        rows = []
        for attrs, card in cards:
            link = re.search(r'\bhref="([^"]+)"', attrs)
            title = re.search(r'<h2\b[^>]*>(.*?)</h2>', card, re.S)
            loc = re.search(r'<div\b[^>]*class="careers-listing-card__location"[^>]*>(.*?)</div>',
                            card, re.S)
            url = html.unescape(link.group(1)) if link else ""
            identity = re.fullmatch(re.escape(BASE) + r'/careers/details/([a-z0-9-]+)/', url)
            if not identity or not title or not text(title.group(1)) or not loc or not text(loc.group(1)):
                raise AdapterError(f"{self.company}: invalid Citadel identity/title/location")
            rows.append(Posting(self.company, self.ats, identity.group(1), text(title.group(1)),
                                text(loc.group(1)), False, "", url))
        if len(rows) != payload["found_posts"] or len({p.job_id for p in rows}) != len(rows):
            raise AdapterError(f"{self.company}: incomplete/duplicate Citadel cards")
        return FetchResult(rows, declared=len(rows),
                           empty_evidence="found_posts=0" if not rows else None)
