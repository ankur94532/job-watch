"""Careers pages with no job API at all: the listing is only in the HTML.

These are the fragile ones. A JSON API that changes shape fails loudly; a web
page whose markup changes keeps answering 200 and a selector simply matches
nothing, which is indistinguishable from a quiet week. So every adapter here
reports three things, and the runner checks all of them (see `_html_checks`):

* the rows it parsed;
* `declared`: the count the page itself states ("Showing 5 of 5 - Jobs",
  "4 Positions"), so a selector that catches only some rows is caught too;
* `empty_evidence`: the page's own words when it says there are no openings.
  Zero rows WITH that text is a genuinely empty board; zero rows WITHOUT it
  means the parser stopped matching.

Structural anchors that must be present on any version of the page (the
openings section, the search box) raise AdapterError when missing: that is a
layout change, and it fails the poll instead of reading as zero.

Both sites allow this in robots.txt (checked 2026-09-18).
"""

import html as htmlmod
import re
from urllib.parse import urljoin

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting


def text(fragment):
    """Visible text of an HTML fragment, whitespace collapsed."""
    t = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()


class MediaNetAdapter(Adapter):
    """careers.media.net: a WordPress site. The home page lists departments,
    each with a count ("1 Position", "4 Positions"); each department page lists
    its openings as <ul class="openings-list"><li><a href=...>Title</a>.

    Only departments that declare openings are fetched, so a normal poll is the
    home page plus one request per non-empty department. Locations are only on
    each job's own page ("Location: New York"), fetched for new rows only.
    """

    ats = "medianet"
    required = ()
    html = True
    HOME = "https://careers.media.net/"

    DEPT = re.compile(r'<a\b([^>]*)>\s*(\d+)\s+Positions?\s*</a>', re.I)
    LIST = re.compile(r'<ul class="openings-list">(.*?)</ul>', re.S)
    ITEM = re.compile(r'<li>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    EMPTY = "We do not have any open positions for this role right now"
    LOCATION = re.compile(r"<b>\s*Location:?\s*</b>(.*?)(?:<br|</p>|<b>)", re.S | re.I)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        home = self.http.get_text(self.HOME)
        if 'id="openings"' not in home:
            raise AdapterError(f"{self.company}: layout changed — no #openings section on {self.HOME}")
        depts = []
        for attrs, n in self.DEPT.findall(home):
            href = re.search(r'href="([^"]+)"', attrs)
            if href and "flex-btn-link" in attrs:
                depts.append((urljoin(self.HOME, href.group(1)), int(n)))
        if not depts:
            raise AdapterError(f"{self.company}: layout changed — no department counts in #openings")

        declared = sum(n for _, n in depts)
        out = []
        for url, n in depts:
            if n == 0:
                continue
            self.pause()
            page = self.http.get_text(url)
            listing = self.LIST.search(page)
            if not listing:
                if self.EMPTY in page:
                    continue      # the count on the home page is stale; the department says none
                raise AdapterError(f"{self.company}: layout changed — {url} declares {n} but has no openings list")
            for href, title in self.ITEM.findall(listing.group(1)):
                job_url = urljoin(url, href)
                slug = job_url.rstrip("/").split("careers.media.net/", 1)[-1]
                out.append(Posting(
                    company=self.company, ats=self.ats, job_id=slug, title=text(title),
                    location="", remote=False, posted_at="", url=job_url, req_id="",
                ))
        evidence = ("every department on the home page says 0 Position"
                    if declared == 0 else None)
        return FetchResult(out, declared=declared, empty_evidence=evidence)

    def needs_enrich(self, posting):
        return not posting.location

    def enrich(self, posting):
        self.pause()
        m = self.LOCATION.search(self.http.get_text(posting.url))
        if m:
            posting.location = text(m.group(1))
            posting.remote = "remote" in posting.location.lower()
        return posting


class SkimaAdapter(Adapter):
    """Skima careers sites (careers.nykaa.com): Astro, server-rendered.

    Each job is a card whose title links to /<uuid>, followed by spans for
    location, workplace (Hybrid / Remote / In Office) and job type. The page
    states "Showing X of Y - Jobs" and, when empty, "Currently there are no job
    postings available". Pagination is a set of buttons carrying
    data-last-page; later pages are requested as ?page=N and only trusted if
    they report data-current-page=N back.
    """

    ats = "skima"
    required = ("host",)
    html = True

    CARD = re.compile(r'<a href="/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
                      r'\s+class="text-lg[^"]*"[^>]*>(.*?)</a>(.*?)(?=<a href="/[0-9a-f-]{36}"\s+class="text-lg|'
                      r'data-pagination-container|$)', re.S)
    SPAN = re.compile(r'<span class="break-all text-sm"[^>]*>(.*?)</span>', re.S)
    SHOWING = re.compile(r"Showing\s+(\d+)\s+of\s+(\d+)\s*-\s*Jobs", re.I)
    EMPTY = "Currently there are no job postings available"
    MAX_PAGES = 20

    def base(self):
        return f"https://{self.cfg['host']}/"

    def fetch(self, ctx=None):
        page = self.http.get_text(self.base())
        if "skima-search-jobs-input" not in page and "Search jobs" not in page:
            raise AdapterError(f"{self.company}: layout changed — no Skima job search on {self.base()}")
        m = self.SHOWING.search(text(page))
        declared = int(m.group(2)) if m else (0 if self.EMPTY in page else None)
        seen, out = set(), []
        self._cards(page, seen, out)

        last = re.search(r'data-last-page="(\d+)"', page)
        for n in range(2, min(int(last.group(1)) if last else 1, self.MAX_PAGES) + 1):
            self.pause()
            more = self.http.get_text(f"{self.base()}?page={n}")
            if f'data-current-page="{n}"' not in more:
                break         # page N is not addressable this way; the declared count will say so
            self._cards(more, seen, out)

        evidence = self.EMPTY if (not out and self.EMPTY in page) else None
        return FetchResult(out, declared=declared, empty_evidence=evidence)

    def _cards(self, page, seen, out):
        for uuid, title, rest in self.CARD.findall(page):
            if uuid in seen:
                continue
            seen.add(uuid)
            spans = [text(s) for s in self.SPAN.findall(rest)]
            location = spans[0] if spans else ""
            workplace = spans[1] if len(spans) > 1 else ""
            out.append(Posting(
                company=self.company, ats=self.ats, job_id=uuid, title=text(title),
                location=location, remote=workplace.lower() == "remote",
                posted_at="", url=self.base() + uuid, req_id="",
            ))


class KulaAdapter(Adapter):
    """Kula careers pages (careers.kula.ai/<account>): Slice.

    The page's own data comes from careers.kula.ai/api/..., which robots.txt
    disallows (`Disallow: /api/`). The listing page itself is allowed and is
    server-rendered with every job, so it is read instead. Its CSS class names
    are generated hashes that change on any rebuild, so nothing keys on them:
    each "chakra-card" is read structurally -- the job link's id, then its
    <p> texts in order: title, location, "Full Time • On-Site".

    `declared` here is the number of distinct job links on the page, found by a
    second, independent selector: if the card reader and the link reader stop
    agreeing, one of them broke.
    """

    ats = "kula"
    required = ("account",)
    html = True

    def base(self):
        return f"https://careers.kula.ai/{self.cfg['account']}"

    def fetch(self, ctx=None):
        page = self.http.get_text(self.base() + "?jobs=true")
        if "chakra-" not in page:
            raise AdapterError(f"{self.company}: layout changed — no Kula (Chakra UI) markup on {self.base()}")
        link = re.compile(r'href="/' + re.escape(self.cfg["account"]) + r'/(\d+)/?[?"]')
        declared = len(set(link.findall(page)))
        out, seen = [], set()
        for card in page.split('<div class="chakra-card')[1:]:
            ids = link.findall(card)
            texts = [text(t) for t in re.findall(r'<p class="chakra-text[^"]*">(.*?)</p>', card, re.S)]
            if not ids or not texts or ids[0] in seen:
                continue
            seen.add(ids[0])
            location = texts[1] if len(texts) > 1 else ""
            kind = texts[2] if len(texts) > 2 else ""
            out.append(Posting(
                company=self.company, ats=self.ats, job_id=ids[0], title=texts[0], location=location,
                remote="remote" in (location + " " + kind).lower(), posted_at="",
                url=f"{self.base()}/{ids[0]}", req_id=ids[0],     # no trailing slash: that one 308s
            ))
        return FetchResult(out, declared=declared)


class GlobalLogicAdapter(Adapter):
    """GlobalLogic's public WordPress listing, filtered server-side to India."""

    ats = "globallogic"
    paginated = True
    html = True
    BASE = "https://www.globallogic.com/career-search-page/"
    MAX_PAGES = 30
    COUNT = re.compile(r"(\d+)\s+Open positions", re.I)
    CARD = re.compile(r'<a href="([^"]+)" class="job_box">(.*?)</a>', re.S)
    TITLE = re.compile(r"<h4>(.*?)</h4>", re.S)
    LOCATION = re.compile(r'<span class="job_location">(.*?)</span>', re.S)
    TAG = re.compile(r'<span class="job_tag">(.*?)</span>', re.S)

    def url(self, page):
        path = "" if page == 1 else f"page/{page}/"
        return f"{self.BASE}{path}?location=india"

    def fetch(self, ctx=None):
        out, seen, declared = [], set(), None
        for page_number in range(1, self.MAX_PAGES + 1):
            if page_number > 1:
                self.pause()
            body = self.http.get_text(self.url(page_number))
            if page_number == 1:
                match = self.COUNT.search(text(body))
                if not match or "career_filter_result" not in body:
                    raise AdapterError(f"{self.company}: layout changed — no open-position count or results")
                declared = int(match.group(1))

            cards = self.CARD.findall(body)
            for url, card in cards:
                title_match = self.TITLE.search(card)
                req_match = re.search(r"IRC\d+", (title_match.group(1) if title_match else "") + url, re.I)
                if not title_match or not req_match:
                    continue
                job_id = url.rstrip("/").rsplit("/", 1)[-1]
                if job_id in seen:
                    continue
                seen.add(job_id)
                locations = [text(value) for value in self.LOCATION.findall(card)]
                tags = [text(value) for value in self.TAG.findall(card)]
                location = " | ".join(dict.fromkeys(locations))
                title_value = text(title_match.group(1))
                req_id = req_match.group(0).upper()
                out.append(Posting(
                    company=self.company, ats=self.ats, job_id=job_id, title=title_value,
                    location=location, remote="remote" in " ".join(tags).lower(), posted_at="",
                    url=url, raw={"locations": locations, "tags": tags}, req_id=req_id,
                ))

            if declared is not None and len(out) >= declared:
                break
            if not cards:
                break
        if declared is None or len(out) != declared:
            raise AdapterError(f"{self.company}: parsed {len(out)} rows but the page declares {declared}")
        return FetchResult(out, declared=declared)
