"""Public job sitemap plus JobPosting JSON-LD, verified against Meta.

The sitemap supplies identity/membership, detail pages supply title/location.
Cache normalized fields so routine polls read only newly discovered jobs and a
bounded rotation of old details. The entire initial scan is resumable. A broken
detail page never masquerades as an empty board. No login or session is needed.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from urllib.parse import urlsplit

from ..http import HttpStatusError, user_agent
from ..robots import fetch as fetch_robots
from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware


class SchemaSitemapAdapter(RobotsAware):
    ats = "schema_sitemap"
    required = ("sitemap", "job_url", "organization")
    paginated = True

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        now = ctx.now if ctx.now is not None else time.time()
        cur = ctx.cursor or {}
        sitemap = self.cfg["sitemap"]
        rules = fetch_robots(sitemap)
        if not rules.allowed(user_agent(), sitemap):
            raise AdapterError(f"{self.company}: robots.txt forbids the job sitemap")
        pattern = re.compile(self.cfg["job_url"])
        if cur:
            urls, offset, records = cur["urls"], cur["offset"], dict(cur["records"])
            ids = list(cur["ids"])
            refresh = set(cur["refresh"])
        else:
            body, _ = self.http.request("GET", sitemap, headers={"Accept": "application/xml"})
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                raise AdapterError(f"{self.company}: invalid job sitemap XML") from e
            urls = []
            for entry in root:
                if entry.tag.rsplit("}", 1)[-1] != "url":
                    continue
                loc = next((c.text for c in entry if c.tag.rsplit("}", 1)[-1] == "loc"), "") or ""
                if pattern.fullmatch(loc) and urlsplit(loc).netloc == urlsplit(sitemap).netloc:
                    urls.append(loc)
            if not urls or len(urls) != len(set(urls)):
                raise AdapterError(f"{self.company}: empty/duplicate job sitemap; refusing closures")
            records = dict(ctx.cache.get("details") or {})
            current = set(urls)
            stale = sorted((r["checked"], u) for u, r in records.items()
                           if u in current and now - r["checked"] >= 7 * 86400)
            refresh = {u for _, u in stale[:int(self.cfg.get("refresh_per_poll") or 10)]}
            offset, ids = 0, []

        out = []

        def checkpoint(i, error=None):
            cursor = {"urls": urls, "offset": i, "records": records,
                      "ids": ids, "refresh": sorted(refresh)}
            return FetchResult(out, complete=False, cursor=cursor, throttled=error)

        for i in range(offset, len(urls)):
            url = urls[i]
            # Cached results are cheap; stop before the next network request.
            needs_fetch = url not in records or url in refresh
            if needs_fetch and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                return checkpoint(i)
            if needs_fetch:
                self.pause()
                try:
                    # Every detail has the same path shape, but check the
                    # actual URL against this sweep's robots rules.
                    if not rules.allowed(user_agent(), url):
                        raise AdapterError(f"{self.company}: robots.txt forbids {url}")
                    page = self.http.get_text(url)
                    job_id = pattern.fullmatch(url).group("req")
                    posting = self.parse_detail(page, url, job_id)
                except HttpStatusError as e:
                    if e.code in (403, 429) or e.code >= 500:
                        return checkpoint(i, e)
                    raise
                records[url] = {"checked": now, "posting": asdict(posting)}
                refresh.discard(url)
            p = Posting(**records[url]["posting"])
            out.append(p)
            ids.append(p.job_id)
        ctx.cache["details"] = {u: records[u] for u in urls}
        return FetchResult(out)

    def parse_detail(self, page, url, job_id):
        jobs = []
        for raw in re.findall(r'<script\b[^>]*type=[\"\x27]application/ld\+json[\"\x27][^>]*>(.*?)</script>', page, re.S | re.I):
            try:
                data = json.loads(raw)
            except ValueError as e:
                raise AdapterError(f"{self.company}: malformed JobPosting JSON-LD") from e
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    jobs.append(item)
        if len(jobs) != 1:
            raise AdapterError(f"{self.company}: expected one JobPosting on {url}, found {len(jobs)}")
        job = jobs[0]
        if (job.get("hiringOrganization") or {}).get("name") != self.cfg["organization"]:
            raise AdapterError(f"{self.company}: unexpected hiring organization on {url}")
        locations = job.get("jobLocation") or []
        if isinstance(locations, dict):
            locations = [locations]
        names = []
        for loc in locations:
            address = loc.get("address") or {}
            if not isinstance(address, dict):
                raise AdapterError(f"{self.company}: JobPosting address is not structured")
            country = address.get("addressCountry") or ""
            if isinstance(country, dict):
                country = country.get("name") or ""
            country = {"IN": "India", "US": "United States"}.get(country, country)
            name = ", ".join(str(v) for v in [address.get("addressLocality"), address.get("addressRegion"), country] if v)
            if name and name not in names:
                names.append(name)
        title = job.get("title")
        if not title or not names:
            raise AdapterError(f"{self.company}: JobPosting lacks title/location on {url}")
        return Posting(company=self.company, ats=self.ats, job_id=job_id, title=title,
                       location=" | ".join(names), remote=job.get("jobLocationType") == "TELECOMMUTE",
                       posted_at=(job.get("datePosted") or "")[:10], url=url, req_id=job_id)
