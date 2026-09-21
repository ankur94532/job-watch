"""Jobvite's public, server-rendered board (verified against Nutanix).

The board contains all jobs in category tables; featured jobs are not part of
the result set. Multi-location rows require their detail page, since "6
Locations" alone cannot establish India eligibility. Only these thin rows need
extra requests. Markup/count failures raise before any closures are recorded.
"""

import re
import time
from urllib.parse import quote, urlencode

from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware
from .html import text


class JobviteAdapter(RobotsAware):
    ats = "jobvite"
    required = ("token",)
    paginated = True
    html = True

    def base(self):
        return "https://jobs.jobvite.com/" + quote(self.cfg["token"], safe="")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = self.base() + "/jobs"
        if self.cfg.get("region"):
            url += "?" + urlencode({"r": self.cfg["region"]})
        self.check_robots(url)
        cur = ctx.cursor or {}
        if cur:
            rows, offset = cur["rows"], cur["offset"]
        else:
            page = self.http.get_text(url)
            rows = self.parse_listing(page)
            offset = 0
        out = []
        for i in range(offset, len(rows)):
            if out and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                return FetchResult(out, complete=False, cursor={
                    "rows": rows, "offset": i,
                    "ids": [r["job_id"] for r in rows[:i]],
                })
            p = Posting(company=self.company, ats=self.ats, remote=False,
                        posted_at="", **rows[i])
            if re.fullmatch(r"\d+\s+Locations?", p.location, re.I):
                self.pause()
                self.check_robots(p.url)
                detail = self.http.get_text(p.url)
                m = re.search(r'<p\b[^>]*class=[\"\x27]jv-job-detail-meta[\"\x27][^>]*>(.*?)</p>',
                              detail, re.S)
                if not m:
                    raise AdapterError(f"{self.company}: no Jobvite detail locations for {p.job_id}")
                parts = [text(s) for s in re.split(r'<span\b[^>]*jv-inline-separator[^>]*>.*?</span>',
                                                  m.group(1), flags=re.S)]
                locations = [s for s in parts[1:] if s and not s.startswith("Req.Num.")]
                if not locations:
                    raise AdapterError(f"{self.company}: empty Jobvite detail locations for {p.job_id}")
                p.location = " | ".join(locations)
                req = re.search(r"Req\.Num\.:\s*(\S+)", text(m.group(1)))
                p.req_id = req.group(1) if req else ""
            p.remote = "remote" in p.location.lower()
            out.append(p)
        return FetchResult(out, declared=len(out))

    def parse_listing(self, page):
        tables = re.findall(r'<table\b[^>]*class=[\"\x27]jv-job-list[\"\x27][^>]*>(.*?)</table>', page, re.S)
        if not tables:
            # Do not infer an empty board from a missing result container.
            raise AdapterError(f"{self.company}: no Jobvite job tables; layout changed or board unavailable")
        result, seen = [], set()
        pattern = re.compile(r'href=[\"\x27](/' + re.escape(self.cfg["token"]) +
                             r'/job/([A-Za-z0-9]+))[\"\x27][^>]*>(.*?)</a>', re.S)
        listing = "".join(tables)
        expected = {m[1] for m in pattern.findall(listing)}
        for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', listing, re.S):
            link = pattern.search(row)
            if not link:
                if re.search(r'<td\b', row):
                    raise AdapterError(f"{self.company}: unrecognized Jobvite result row")
                continue
            loc = re.search(r'<td\b[^>]*class=[\"\x27]jv-job-list-location[\"\x27][^>]*>(.*?)</td>', row, re.S)
            title = text(link.group(3))
            if not loc or not text(loc.group(1)) or not title:
                raise AdapterError(f"{self.company}: Jobvite row missing title/location")
            job_id = link.group(2)
            if job_id in seen:
                raise AdapterError(f"{self.company}: duplicate Jobvite job {job_id}")
            seen.add(job_id)
            result.append({"job_id": job_id, "title": title, "location": text(loc.group(1)),
                           "url": "https://jobs.jobvite.com" + link.group(1), "req_id": ""})
        if not result or seen != expected:
            raise AdapterError(f"{self.company}: incomplete Jobvite listing")
        if re.search(r'class=[\"\x27][^\"\x27]*\b(?:jv-pagination|pagination)\b', page):
            raise AdapterError(f"{self.company}: unexpected Jobvite pagination; refusing partial board")
        return result
