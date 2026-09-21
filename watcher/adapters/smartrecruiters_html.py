"""Counted SmartRecruiters career-page HTML, without the separate REST API.

Country search exposes all matching location groups and each group's count.
The published Show more route supplies remaining cards (nine initially, ten
per subsequent page). Hold rows in the cursor until a final index read agrees
with the starting counts and heads, so incomplete or moving lists cannot close
jobs. This source is independent of LinkedIn's own Jobs platform.
"""

import copy
import html
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware
from .html import text


def attrs(tag):
    return {key: html.unescape(value) for key, value in
            re.findall(r'([\w-]+)="([^"]*)"', tag)}


class SmartRecruitersHTMLAdapter(RobotsAware):
    ats = "smartrecruiters_html"
    required = ("token", "organization", "country")
    html = True
    paginated = True
    EMPTY = "No job postings are currently available."

    @property
    def base(self):
        return "https://careers.smartrecruiters.com/" + quote(self.cfg["token"], safe="")

    def url(self):
        return self.base + "?" + urlencode({"search": self.cfg["country"]})

    def more_url(self, group, page):
        return self.base + "/api/more?" + urlencode({
            "search": self.cfg["country"], "type": "location",
            "value": group["value"], "page": page})

    def cards(self, markup, location):
        rows = []
        for tag, body in re.findall(r'(<a\b[^>]*>)(.*?)</a>', markup, re.S):
            a = attrs(tag)
            if "js-job-ad-link" not in a.get("class", "").split():
                continue
            url = a.get("href", "")
            parts = urlsplit(url)
            identity = re.fullmatch("/" + re.escape(self.cfg["token"]) + r"/(\d+)-[^/]+", parts.path)
            titles = re.findall(r'<h4\b[^>]*>(.*?)</h4>', body, re.S)
            if (parts.scheme != "https" or parts.netloc != "jobs.smartrecruiters.com"
                    or parts.query or parts.fragment or not identity
                    or len(titles) != 1 or not text(titles[0])):
                raise AdapterError(f"{self.company}: invalid SmartRecruiters job card")
            ref = re.search(r' - ((?:REQ|REF)[A-Za-z0-9_-]+)$', a.get("aria-label", ""))
            rows.append({"job_id": identity.group(1), "title": text(titles[0]),
                         "location": location, "url": url,
                         "req_id": ref.group(1) if ref else ""})
        if len(rows) != len(re.findall(r'class="[^"]*\bjs-job-ad-link\b', markup)):
            raise AdapterError(f"{self.company}: unparsed SmartRecruiters cards")
        if len({r["job_id"] for r in rows}) != len(rows):
            raise AdapterError(f"{self.company}: duplicate SmartRecruiters page IDs")
        return rows

    def index(self, markup):
        titles = re.findall(r'<title\b[^>]*>(.*?)</title>', markup, re.S)
        if titles != ["Careers at " + self.cfg["organization"]]:
            raise AdapterError(f"{self.company}: wrong SmartRecruiters organization")
        sections = [(attrs(tag), body) for tag, body in
                    re.findall(r'(<section\b[^>]*\bdata-qty="[^"]*"[^>]*>)(.*?)</section>', markup, re.S)
                    if "js-group" in attrs(tag).get("class", "").split()]
        group_count = sum("js-group" in attrs(tag).get("class", "").split()
                          for tag in re.findall(r'<section\b[^>]*>', markup))
        if len(sections) != group_count:
            raise AdapterError(f"{self.company}: unparsed SmartRecruiters location group")
        if not sections:
            empty = re.findall(r'<p\b[^>]*id="st-noOpeningsFound"[^>]*>(.*?)</p>', markup, re.S)
            if list(map(text, empty)) == [self.EMPTY] and "js-job-ad-link" not in markup:
                return []
            raise AdapterError(f"{self.company}: missing SmartRecruiters groups/empty evidence")
        loaders = [attrs(t) for t in re.findall(r'<div\b[^>]*>', markup)
                   if "js-openings-load" in attrs(t).get("class", "").split()]
        try:
            complete_groups = (len(loaders) == 1 and
                              int(loaders[0]["data-page"]) >= int(loaders[0]["data-groups-pages"]) - 1)
        except (KeyError, ValueError):
            complete_groups = False
        if not complete_groups:
            raise AdapterError(f"{self.company}: SmartRecruiters location groups are incomplete")
        groups, locations = [], set()
        for a, body in sections:
            headings = re.findall(r'<h3\b[^>]*>(.*?)</h3>', body, re.S)
            if len(headings) != 1 or not text(headings[0]) or not a.get("data-qty", "").isdigit():
                raise AdapterError(f"{self.company}: missing SmartRecruiters location/count")
            location, total = text(headings[0]), int(a["data-qty"])
            if location in locations or total < 1:
                raise AdapterError(f"{self.company}: invalid SmartRecruiters group")
            locations.add(location)
            rows = self.cards(body, location)
            counts = re.findall(r'<span\b[^>]*class="title"[^>]*>\s*(\d+) jobs?\s*</span>', body)
            if counts != [str(total)]:
                raise AdapterError(f"{self.company}: inconsistent SmartRecruiters group count")
            links = [attrs(t) for t in re.findall(r'<a\b[^>]*>', body)
                     if "js-more" in attrs(t).get("class", "").split()]
            if len(rows) != min(total, 9) or len(links) != int(total > 9):
                raise AdapterError(f"{self.company}: incomplete SmartRecruiters group head")
            value = ""
            if links:
                link = links[0]
                if (link.get("data-type") != "location" or link.get("data-page") != "0"
                        or link.get("data-more") != "/" + self.cfg["token"] + "/api/more"
                        or not link.get("data-value")):
                    raise AdapterError(f"{self.company}: unsafe SmartRecruiters pagination")
                value = unquote(link["data-value"])
            if location.casefold().endswith(", " + self.cfg["country"].casefold()):
                groups.append({"location": location, "total": total, "value": value,
                               "head": rows, "rows": list(rows)})
        if not groups:
            raise AdapterError(f"{self.company}: country search returned no matching location groups")
        return groups

    @staticmethod
    def signature(groups):
        return [(g["location"], g["total"], g["value"], g["head"]) for g in groups]

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        scope = [self.cfg[k] for k in self.required]
        cur = copy.deepcopy(ctx.cursor) if ctx.cursor else {
            "v": 1, "scope": scope, "groups": None, "group": 0, "page": 1}
        if cur.get("v") != 1 or cur.get("scope") != scope:
            return FetchResult([], complete=False, restart=True)
        requests = 0
        self.check_robots(self.url())
        self.check_robots(self.base + "/api/more")
        while True:
            if requests and (requests >= int(self.cfg.get("max_requests_per_run", 12)) or
                             (ctx.deadline is not None and time.monotonic() >= ctx.deadline)):
                return FetchResult([], complete=False, cursor=cur)
            groups = cur["groups"]
            if groups is not None:
                while cur["group"] < len(groups) and len(groups[cur["group"]]["rows"]) == groups[cur["group"]]["total"]:
                    cur["group"] += 1
                    cur["page"] = 1
            initial = groups is None
            verify = not initial and cur["group"] == len(groups)
            url = self.url() if initial or verify else self.more_url(groups[cur["group"]], cur["page"])
            if requests:
                self.pause()
            try:
                markup = self.http.get_text(url)
            except HttpStatusError as e:
                if e.code in (403, 429):
                    return self.partial_on_throttle(e, [], cur)
                raise
            requests += 1
            if initial:
                cur["groups"] = self.index(markup)
            elif verify:
                if self.signature(self.index(markup)) != self.signature(groups):
                    return FetchResult([], complete=False, restart=True)
                rows = [row for g in groups for row in g["rows"]]
                if len(rows) != sum(g["total"] for g in groups) or len({r["job_id"] for r in rows}) != len(rows):
                    raise AdapterError(f"{self.company}: incomplete/duplicate SmartRecruiters inventory")
                return FetchResult([Posting(self.company, self.ats, remote=False, posted_at="", **r)
                                    for r in rows], declared=len(rows),
                                   empty_evidence=self.EMPTY if not rows else None)
            else:
                g = groups[cur["group"]]
                rows = self.cards(markup, g["location"])
                if {r["job_id"] for r in rows}.intersection(r["job_id"] for r in g["rows"]):
                    return FetchResult([], complete=False, restart=True)
                if len(rows) != min(10, g["total"] - len(g["rows"])):
                    raise AdapterError(f"{self.company}: incomplete SmartRecruiters continuation page")
                g["rows"].extend(rows)
                cur["page"] += 1
