"""MathWorks' first-party, full-board XML job feed."""

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from urllib.parse import urlsplit

from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware


FEED = "https://www.mathworks.com/company/jobs/opportunities/rss.xml"
JOB_PATH = re.compile(r"/company/jobs/opportunities/(?P<id>\d+)(?:-[^/]+)?\.html")


class MathWorksAdapter(RobotsAware):
    ats = "mathworks"
    required = ("country",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = self.cfg.get("feed") or FEED
        self.check_robots(url)
        body, _ = self.http.request(
            "GET", url, headers={"Accept": "application/rss+xml,application/xml,text/xml"})
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise AdapterError(f"{self.company}: MathWorks feed is not valid XML: {exc}") from exc

        channel = root.find("channel") if root.tag == "rss" else None
        if (channel is None or (channel.findtext("title") or "").strip() != "MathWorks Job Postings"
                or (channel.findtext("link") or "").strip()
                != "https://www.mathworks.com/company/jobs/opportunities/"):
            raise AdapterError(f"{self.company}: mismatched MathWorks feed identity")

        target = self.cfg["country"].strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", target):
            raise AdapterError(f"{self.company}: country must be a two-letter code")

        grouped = OrderedDict()
        pairs = set()
        items = channel.findall("item")
        if not items:
            raise AdapterError(f"{self.company}: MathWorks feed has no job items")
        for raw in items:
            job_id = (raw.findtext("id") or "").strip()
            title = (raw.findtext("title_raw") or "").strip()
            link = (raw.findtext("link") or "").strip()
            location = (raw.findtext("./location/locationName") or "").strip()
            country = (raw.findtext("./location/country") or "").strip().upper()
            parts = urlsplit(link)
            match = JOB_PATH.fullmatch(parts.path)
            if (not job_id.isdigit() or not title or not location
                    or not re.fullmatch(r"[A-Z]{2}", country)
                    or parts.scheme != "https" or parts.netloc != "www.mathworks.com"
                    or parts.query or parts.fragment or not match
                    or match.group("id") != job_id):
                raise AdapterError(f"{self.company}: malformed MathWorks job item")

            pair = (job_id, location, country)
            previous = grouped.get(job_id)
            if previous and (previous["title"] != title or previous["url"] != link):
                raise AdapterError(f"{self.company}: conflicting duplicate MathWorks job {job_id}")
            if pair in pairs:
                # The live feed currently repeats a few items byte-for-byte.
                continue
            pairs.add(pair)
            if previous is None:
                previous = grouped[job_id] = {
                    "title": title, "url": link, "locations": [], "countries": []}
            previous["locations"].append(location)
            previous["countries"].append(country)

        out = []
        for job_id, raw in grouped.items():
            locations = [location for location, country in
                         zip(raw["locations"], raw["countries"]) if country == target]
            if not locations:
                continue
            location = join_locations(locations)
            out.append(Posting(
                company=self.company,
                ats=self.ats,
                job_id=job_id,
                title=raw["title"],
                location=location,
                remote="remote" in location.lower(),
                posted_at="",
                url=raw["url"],
                raw={"locations": list(raw["locations"]),
                     "countries": list(raw["countries"])},
                req_id=job_id,
            ))
        if not out:
            raise AdapterError(f"{self.company}: MathWorks feed has no {target} jobs")
        return FetchResult(out)
