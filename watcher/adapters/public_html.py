"""Counted public HTML lists for Trakstar and SAP SuccessFactors career sites.

Follow only pagination URLs published by the site, retaining progress on
throttling. Remote text never substitutes for a named eligible location.
"""

import html
import re
import logging
import time
from urllib.parse import urljoin, urlsplit

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware
from .html import text


log = logging.getLogger(__name__)


class CountedHTML(RobotsAware):
    required = ("host", "organization")
    paginated = True
    html = True
    path = "/"

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        base = "https://" + self.cfg["host"]
        start_path = self.cfg.get("path") or self.path
        if not start_path.startswith("/"):
            raise AdapterError(f"{self.company}: HTML search path must be absolute")
        url, total = cur.get("url", base + start_path), cur.get("total")
        ids, visited = set(cur.get("ids", [])), set(cur.get("visited", []))
        # Rows SERVED, which is what the board's own first/last range counts.
        # It is not len(ids): a board may serve the same job in two slots.
        slots = int(cur.get("slots", len(ids)))
        out, pages = [], 0

        def cursor():
            return {"url": url, "total": total, "ids": sorted(ids),
                    "visited": sorted(visited), "slots": slots}

        while url:
            if pages and ((ctx.deadline is not None and time.monotonic() >= ctx.deadline) or pages >= 10):
                return FetchResult(out, complete=False, cursor=cursor(), declared=len(out))
            if urlsplit(url).netloc != self.cfg["host"] or urlsplit(url).scheme != "https" or url in visited:
                raise AdapterError(f"{self.company}: unsafe/repeated pagination link")
            self.check_robots(url)
            if pages:
                self.pause()
            try:
                page = self.http.get_text(url)
            except HttpStatusError as e:
                if e.code in (403, 429):
                    return self.partial_on_throttle(e, out, cursor())
                raise
            rows, count, first, last, next_url = self.parse(page)
            # Some boards re-count between requests while serving a perfectly
            # coherent sequence. Arm flickered 104, 104, 102 across three pages
            # on 2026-10-13 with contiguous, non-overlapping ranges, and any
            # change restarted the sweep, so it never completed for 13h.
            # `count_drift` opts a company into adopting the newer total; it
            # defaults to 0, so every other board still restarts on any change.
            # Nothing else is relaxed: the first/last range, the overlap check
            # and the final len(ids) == total all still have to hold, so a page
            # that actually skips or repeats rows still fails.
            if total is not None and total != count:
                drift = int(self.cfg.get("count_drift") or 0)
                if abs(count - total) > drift:
                    return FetchResult([], complete=False, restart=True, declared=0)
                log.warning("%s: declared count moved %d -> %d mid-sweep; within the "
                            "configured drift of %d", self.company, total, count, drift)
            total = count
            keys = [p.job_id for p in rows]
            if len(keys) != len(set(keys)):
                raise AdapterError(f"{self.company}: duplicate page IDs")
            repeats = ids.intersection(keys)
            if repeats and len(repeats) > int(self.cfg.get("repeat_tolerance") or 0):
                # Too many repeats to be the board merely re-sorting: the
                # pagination is no longer trustworthy, so start over.
                return FetchResult([], complete=False, restart=True, declared=0)
            if repeats:
                # Within tolerance: the board re-sorted and spent a slot on a
                # row we already hold. Keep paging on the SLOT counter so the
                # range stays checkable, and simply do not store it twice.
                log.warning("%s: %d repeated row(s) at slot %d of %d; keeping the "
                            "first copy", self.company, len(repeats), first, count)
            if count < 1 or first != slots + 1 or last != slots + len(rows) or last > count:
                raise AdapterError(f"{self.company}: incomplete HTML result range")
            if bool(next_url) != (last < count):
                raise AdapterError(f"{self.company}: missing/inconsistent next page")
            ids.update(keys)
            out.extend(r for r in rows if r.job_id not in repeats)
            slots += len(rows)
            visited.add(url)
            url = urljoin(url, html.unescape(next_url)) if next_url else None
            pages += 1
        if slots != total:
            raise AdapterError(f"{self.company}: incomplete HTML inventory")
        # Every slot the board promised was served and the range never broke,
        # so the sweep IS complete -- there are simply fewer distinct jobs than
        # slots when the board repeats one. `declared` reports what was stored,
        # which is what the runner compares against.
        if len(ids) != total:
            log.warning("%s: %d slots held %d distinct jobs", self.company, total, len(ids))
        return FetchResult(out, declared=len(out))


class TrakstarAdapter(CountedHTML):
    ats = "trakstar"

    def parse(self, page):
        name = re.search(r'<h1\b[^>]*class="company-header-title"[^>]*>(.*?)</h1>', page, re.S)
        if not name or text(name.group(1)) != "Jobs at " + self.cfg["organization"]:
            raise AdapterError(f"{self.company}: wrong Trakstar company")
        paginator = re.search(r'\bpaginator:\s*\{([^}]+)\}', page)
        if not paginator:
            raise AdapterError(f"{self.company}: missing Trakstar pagination")
        fields = dict(re.findall(r"(\w+):\s*(null|'[^']*'|\d+)", paginator.group(1)))
        try:
            first, last, total = (int(fields[k].strip("'")) for k in ('start_index', 'end_index', 'total_results'))
            nxt = fields['next_page_url']
        except (KeyError, ValueError) as e:
            raise AdapterError(f"{self.company}: invalid Trakstar pagination") from e
        nxt = None if nxt == 'null' else nxt.strip("'")
        cards = re.findall(r'<div\b[^>]*class="[^"]*js-careers-page-job-list-item[^\"]*"[^>]*>\s*<a\b[^>]*href="(/jobs/([A-Za-z0-9]+)/)"[^>]*>(.*?)</a>', page, re.S)
        out = []
        for path, job_id, card in cards:
            title = re.search(r'<h3\b[^>]*>(.*?)</h3>', card, re.S)
            location = re.search(r'<div\b[^>]*class="[^"]*js-job-list-opening-loc[^\"]*"[^>]*title="([^"]+)"', card, re.S)
            if not title or not location or not text(title.group(1)) or not html.unescape(location.group(1)).strip():
                raise AdapterError(f"{self.company}: missing Trakstar title/location")
            loc = html.unescape(location.group(1)).strip()
            out.append(Posting(self.company, self.ats, job_id, text(title.group(1)), loc,
                               'remote' in loc.lower(), '', 'https://' + self.cfg['host'] + path))
        return out, total, first, last, nxt


class SuccessFactorsHTMLAdapter(CountedHTML):
    ats = "successfactors_html"
    path = "/search/"

    def parse(self, page):
        title = re.search(r'<title[^>]*>(.*?)</title>', page, re.S)
        if not title or self.cfg['organization'] not in text(title.group(1)):
            raise AdapterError(f"{self.company}: wrong SuccessFactors company")
        labels = re.findall(r'<span\b[^>]*class="paginationLabel"[^>]*>(.*?)</span>', page, re.S)
        counts = {tuple(map(int, m)) for label in labels for m in
                  re.findall(r'Results\s+(\d+)\s*[–-]\s*(\d+)\s+of\s+(\d+)', text(label).replace(',', ''))}
        if len(counts) != 1:
            raise AdapterError(f"{self.company}: missing SuccessFactors result count")
        first, last, total = counts.pop()
        rows = re.findall(r'<tr\b[^>]*class="[^"]*data-row[^\"]*"[^>]*>(.*?)</tr>', page, re.S)
        out = []
        for row in rows:
            link = re.search(r'<a\b[^>]*href="(/job/[^\"]+/(\d+)/)"[^>]*>(.*?)</a>', row, re.S)
            locations = re.findall(r'<span\b[^>]*class="jobLocation(?: [^\"]*)?"[^>]*>(.*?)</span>', row, re.S)
            places = list(dict.fromkeys(text(loc) for loc in locations if text(loc)))
            if not link or not text(link.group(3)) or len(places) != 1:
                raise AdapterError(f"{self.company}: missing/ambiguous SuccessFactors title/location")
            out.append(Posting(self.company, self.ats, link.group(2), text(link.group(3)), places[0],
                               'remote' in places[0].lower(), '', 'https://' + self.cfg['host'] +
                               html.unescape(link.group(1)), req_id=link.group(2)))
        links = [html.unescape(u) for u in re.findall(r'href="([^"]+)"', page)]
        nxt = next((u for u in links if re.search(r'[?&]startrow=' + str(last) + r'(?:&|$)', u)), None)
        return out, total, first, last, nxt
