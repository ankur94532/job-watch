"""Radancy / TalentBrew career sites, read from their XML sitemap (Intuit).

Intuit's board (`jobs.intuit.com`, Radancy TalentBrew over Avature) has no JSON
API: `/search-jobs/results` returns a JSON envelope whose `results` field is a
blob of HTML, which is scraping by another name. What it does publish is a
standard `sitemap.xml` — a machine-readable feed meant to be fetched by
crawlers — and every job URL in it carries all the fields needed:

    <loc>https://jobs.intuit.com/job/bengaluru/staff-software-engineer/27595/100674391344</loc>
    <lastmod>2026-09-16T04:01:26Z</lastmod>
                     ^city        ^title slug              ^client  ^requisition id

Verified 2026-09-16: 512 job URLs, 34 of them in Bengaluru, `lastmod` on all.

Consequences worth knowing, because they are weaker than a real API:

* **Titles come from the URL slug**, so casing and punctuation are lost:
  "staff-software-engineer-traffic" → "Staff Software Engineer Traffic".
  Filters normalise punctuation anyway, so matching is unaffected.
* **Location is the city slug only** ("bengaluru"), with no country or region.
* `lastmod` is when the posting last changed, not when it opened — close enough
  to rank on, and new reqs are detected by id regardless.
* **Coverage is whatever the sitemap lists.** Intuit's search UI implies more
  open reqs than the 512 here, so treat this as a good subset, not the board.
* No ETag on the sitemap, and it is ~330KB, so this defaults to hourly.

Only verified against Intuit — `careers.walgreens.com` and `jobs.cvshealth.com`
publish nothing comparable, so do not assume it generalises to every TalentBrew
site without checking.
"""

import re
import xml.etree.ElementTree as ET

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
# /job/<city>/<title-slug>/<client id>/<requisition id>
JOB_URL = re.compile(r"/job/(?P<city>[^/]+)/(?P<slug>[^/]+)/(?P<client>\d+)/(?P<req>\d+)/?$")


class RadancyAdapter(Adapter):
    ats = "radancy"
    required = ("host",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = self.cfg.get("sitemap") or f"https://{self.cfg['host']}/sitemap.xml"
        body, _ = self.http.request("GET", url, headers={"Accept": "application/xml,text/xml"})
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise AdapterError(f"{self.company}: sitemap is not valid XML: {e}") from e

        out, seen = [], set()
        for entry in root.iter(f"{SITEMAP_NS}url"):
            loc = entry.findtext(f"{SITEMAP_NS}loc") or ""
            m = JOB_URL.search(loc)
            if not m or m.group("req") in seen:
                continue
            seen.add(m.group("req"))
            lastmod = (entry.findtext(f"{SITEMAP_NS}lastmod") or "")[:10]
            out.append(
                Posting(
                    company=self.company,
                    ats=self.ats,
                    job_id=m.group("req"),
                    title=unslug(m.group("slug")),
                    location=unslug(m.group("city")),
                    remote="remote" in m.group("city").lower(),
                    posted_at=lastmod,
                    url=loc,
                    raw={"loc": loc, "lastmod": lastmod, "city": m.group("city"),
                         "slug": m.group("slug"), "client": m.group("client")},
                    req_id=m.group("req"),
                )
            )
        if not out:
            raise AdapterError(f"{self.company}: no job URLs in {url} — has the URL shape changed?")
        return FetchResult(out)


def unslug(slug):
    """"staff-software-engineer" -> "Staff Software Engineer" (the slug is all we get)."""
    return " ".join(w.capitalize() for w in (slug or "").replace("_", "-").split("-") if w)
