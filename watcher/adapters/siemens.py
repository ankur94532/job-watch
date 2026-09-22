"""Siemens' permitted India-filtered HTML search, six rows/page.

Persist the current page as well as resolved IDs so a detail throttle cannot
skip a multi-location row. Complete only against the displayed result count.
The country facet can include jobs whose detail lists only other countries.
Keep their verified locations and count them in the search inventory; normal
downstream filtering decides India eligibility, not search membership alone.
Request the frontend's explicit oldest-first ordering: the default unsorted
search moves rows across pages even when its total is unchanged. Count and
overlap guards remain necessary because ordering is not snapshot isolation.
"""

import logging
import re
import time
from urllib.parse import urlencode

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware
from .html import text

BASE = "https://jobs.siemens.com/en_US/externaljobs/"
ORDER = "postedDate:ASC"
log = logging.getLogger(__name__)


class SiemensAdapter(RobotsAware):
    ats = "siemens"
    paginated = True
    html = True
    # A known multi-location row was resolved before it entered the store, and
    # Store.record deliberately preserves an existing row's location. Fetching
    # that detail again cannot change stored data; only new IDs need it.
    needs_known_ids = True

    def url(self, offset):
        return BASE + "SearchJobs/?" + urlencode({"42386": "[812053]", "42386_format": "17546",
            "listFilterMode": "1", "folderRecordsPerPage": 6, "folderOffset": offset,
            "folderSort": "postedDate", "folderSortDirection": "ASC"})

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}

        def restart_cursor():
            return {"offset": 0, "total": None, "pending": [], "ids": [], "order": ORDER}

        if cur and cur.get("order") != ORDER:
            return FetchResult([], complete=False, cursor=restart_cursor(), restart=True, declared=0)
        offset, total = cur.get("offset", 0), cur.get("total")
        pending, ids = list(cur.get("pending", [])), set(cur.get("ids", []))
        if len(ids) + len(pending) != offset:
            raise AdapterError(f"{self.company}: invalid Siemens resume count")
        self.check_robots(self.url(offset))
        out, requests = [], 0

        def cursor():
            return {"offset": offset, "total": total, "pending": pending,
                    "ids": sorted(ids), "order": ORDER}

        while True:
            if not pending and total is not None and offset == total:
                if len(ids) != total:
                    raise AdapterError(f"{self.company}: incomplete Siemens inventory")
                return FetchResult(out, declared=len(out),
                                   empty_evidence="No jobs found" if total == 0 else None)
            if requests and ((ctx.deadline is not None and time.monotonic() >= ctx.deadline)
                             or requests >= int(self.cfg.get("max_requests_per_run", 16))):
                return FetchResult(out, complete=False, cursor=cursor(), declared=len(out))
            try:
                if not pending:
                    if requests:
                        self.pause()
                    requests += 1
                    rows, count = self.parse_listing(self.http.get_text(self.url(offset)), offset)
                    if total is not None and total != count:
                        if count < total:
                            log.warning("%s: Siemens count fell %d -> %d at offset %d; restarting",
                                        self.company, total, count, offset)
                            return FetchResult([], complete=False, cursor=restart_cursor(),
                                               restart=True, declared=0)
                        # The requested order is oldest-first. New postings at
                        # the end can safely extend a sweep already in progress.
                        # If an insertion landed before the cursor instead, it
                        # shifts a prior ID onto this page and the overlap guard
                        # below restarts the sweep.
                        log.info("%s: Siemens count grew %d -> %d at offset %d; extending sweep",
                                 self.company, total, count, offset)
                        total = count
                    else:
                        total = count
                    keys = [r["job_id"] for r in rows]
                    if len(set(keys)) != len(keys):
                        raise AdapterError(f"{self.company}: duplicate Siemens page IDs")
                    if ids.intersection(keys):
                        log.warning("%s: Siemens page overlaps prior IDs at offset %d; restarting",
                                    self.company, offset)
                        return FetchResult([], complete=False, cursor=restart_cursor(),
                                           restart=True, declared=0)
                    pending = rows
                    offset += len(rows)
                    if not pending:
                        continue
                row = dict(pending[0])
                if (re.search(r"multiple|\d+\s+locations?", row["location"], re.I)
                        and row["job_id"] not in (ctx.known or set())):
                    if requests >= int(self.cfg.get("max_requests_per_run", 16)):
                        return FetchResult(out, complete=False, cursor=cursor(), declared=len(out))
                    self.pause()
                    self.check_robots(row["url"])
                    requests += 1
                    row["location"] = self.detail_location(self.http.get_text(row["url"]), row["job_id"])
                out.append(Posting(self.company, self.ats, remote=False, posted_at="", **row))
                ids.add(row["job_id"])
                pending.pop(0)
            except HttpStatusError as e:
                if e.code in (403, 429):
                    return self.partial_on_throttle(e, out, cursor())
                raise

    def parse_listing(self, page, offset):
        if (re.findall(r'"jobSort"\s*:\s*"([^"]*)"', page) != ["postedDate"]
                or re.findall(r'"jobSortDirection"\s*:\s*"([^"]*)"', page) != ["ASC"]):
            raise AdapterError(f"{self.company}: Siemens did not confirm requested ordering")
        legends = re.findall(r'<div\b[^>]*class="list-controls__text__legend[^\"]*"[^>]*>(.*?)</div>', page, re.S)
        counts = {tuple(map(int, m)) for legend in legends
                  for m in re.findall(r'(\d+)\s*-\s*(\d+)\s+of\s+(\d+)', text(legend).replace(',', ''))}
        articles = re.findall(r'<article\b[^>]*class="[^"]*article--result[^\"]*"[^>]*>(.*?)</article>', page, re.S)
        if not counts and not articles and re.search(r'No jobs (?:found|match)', text(page), re.I) and offset == 0:
            return [], 0
        if len(counts) != 1:
            raise AdapterError(f"{self.company}: missing/ambiguous Siemens result count")
        first, last, total = counts.pop()
        if first != offset + 1 or last > total or last < first or len(articles) != last - offset:
            raise AdapterError(f"{self.company}: incomplete Siemens page at {offset}/{total}")
        rows = []
        for article in articles:
            headings = "".join(re.findall(r'<h3\b[^>]*>(.*?)</h3>', article, re.S))
            links = re.findall(r'<a\b[^>]*href="https://jobs\.siemens\.com/en_US/externaljobs/JobDetail/(\d+)"[^>]*>(.*?)</a>', headings, re.S)
            title_links = [(job_id, text(label)) for job_id, label in links if text(label)]
            if len(title_links) != 1:
                raise AdapterError(f"{self.company}: missing/ambiguous Siemens job link")
            job_id, title = title_links[0]
            places = re.findall(r'<span\b[^>]*class="list-item-job(?:City|State|Country)"[^>]*>(.*?)</span>', article, re.S)
            location = ", ".join(dict.fromkeys(text(p) for p in places if text(p)))
            if not location:
                loc = re.search(r'<span\b[^>]*class="list-item-location"[^>]*>(.*?)</span>', article, re.S)
                location = text(loc.group(1)) if loc else ""
            identity = re.search(r'<span\b[^>]*class="list-item-jobId"[^>]*>\s*Job ID:\s*(\d+)\s*</span>', article)
            if not location or not identity or identity.group(1) != job_id:
                raise AdapterError(f"{self.company}: incomplete Siemens job {job_id}")
            rows.append({"job_id": job_id, "title": title, "location": location,
                         "url": BASE + "JobDetail/" + job_id, "req_id": job_id})
        if last < total and not re.search(r'folderOffset=' + str(last) + r'(?:&|["\x27])', page):
            raise AdapterError(f"{self.company}: Siemens next-page link missing")
        return rows, total

    def detail_location(self, page, job_id):
        fields = re.findall(r'<div\b[^>]*class="article__content__view__field__label"[^>]*>(.*?)</div>\s*'
                            r'<div\b[^>]*class="article__content__view__field__value"[^>]*>(.*?)</div>', page, re.S)
        identities = [text(value) for label, value in fields if text(label) == "Job ID"]
        if identities != [job_id]:
            raise AdapterError(f"{self.company}: Siemens detail identity mismatch")
        values = [value for label, value in fields
                  if text(label) in ("Location(s)", "Any Siemens location in")]
        plain = False
        if not values:
            # A small number of global/roaming roles omit the usual label but
            # retain a structured Location field. Keep its actual scope so an
            # India filter does not mistake search-facet membership for proof.
            values = re.findall(
                r'<div\b[^>]*class="article__content__view__field__value"[^>]*>\s*'
                r'<div\b[^>]*>\s*<strong\b[^>]*>\s*Location:\s*</strong>(.*?)</div>\s*</div>',
                page, re.S | re.I)
            plain = True
        if len(values) != 1:
            raise AdapterError(f"{self.company}: missing/ambiguous Siemens detail locations")
        value = values[0]
        locations = [text(s) for s in re.findall(r'<li\b[^>]*>(.*?)</li>', value, re.S)]
        if plain and not locations:
            locations = [text(value)]
        if not locations or not all(locations):
            raise AdapterError(f"{self.company}: missing Siemens detail locations")
        return " | ".join(dict.fromkeys(locations))
