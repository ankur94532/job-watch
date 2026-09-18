"""A company that publishes its own job JSON, found by tracing its careers SPA.

Quadeye names no ATS in its HTML or bundles; `watcher trace` caught the XHR.

(PhonePe was found the same way and then dropped from here: the applyUrl in its
feed pointed at jobs.smartrecruiters.com/PHONEPELIMITED, so it is a plain
SmartRecruiters board — with requisition numbers, release dates and ETag support
that its own feed lacks. Earlier probes had guessed the tokens "phonepe" and
"PhonePe", not the legal-entity spelling.)

D. E. Shaw India
----------------
A Next.js site: `GET https://www.deshawindia.com/_next/data/{buildId}/en/careers.json`
carries `pageProps.regularJobs[]` — 82 rows with `data.displayName`,
`data.jobUrl`, `id`, and `office[]` / `data.jobMetadata.jobLocations[]`.

**The buildId rotates on every deploy**, so it is read from the careers page on
each fetch rather than stored in config — two requests per poll, and a redeploy
between them just means the next poll succeeds.

Quadeye
-------
`GET https://www.quadeye.com/api/zoho-recruit/job-openings` — 200, no auth, no
robots rule. Their own proxy in front of Zoho Recruit, so the rows are Zoho's
shape: `Posting_Title`, `City`, `Job_Opening_ID`, `Remote_Job`.
"""

import re

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

QUADEYE_API = "https://www.quadeye.com/api/zoho-recruit/job-openings"
DESHAW_PAGE = "https://www.deshawindia.com/careers/current-openings"
DESHAW_DATA = "https://www.deshawindia.com/_next/data/{build}/en/careers.json"
BUILD_ID = re.compile(r'"buildId":"([^"]+)"')


class QuadeyeAdapter(Adapter):
    ats = "quadeye"

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(QUADEYE_API, cache=ctx.cache)
        rows = data.get("data")
        if rows is None:
            raise AdapterError(f"{self.company}: response has no 'data' — shape changed?")
        return FetchResult([self._posting(r) for r in rows])

    def _posting(self, raw):
        jid = str(raw.get("Job_Opening_ID") or raw.get("id") or "")
        places = [raw.get("City"), raw.get("State"), raw.get("Country")]
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=jid,
            title=(raw.get("Posting_Title") or raw.get("Job_Opening_Name") or "").strip(),
            location=join_locations([p for p in places if p]),
            remote=str(raw.get("Remote_Job") or "").lower() in ("true", "yes"),
            posted_at=(raw.get("Date_Opened") or "")[:10],
            url=raw.get("url") or f"https://www.quadeye.com/careers/{jid}",
            raw={k: v for k, v in raw.items() if k not in ("Job_Description",)},
            req_id=jid,
        )


class DEShawAdapter(Adapter):
    ats = "deshaw"

    def build_id(self):
        body, _ = self.http.request("GET", DESHAW_PAGE, headers={"Accept": "text/html"})
        m = BUILD_ID.search(body.decode("utf-8", "replace"))
        if not m:
            raise AdapterError(f"{self.company}: no buildId on the careers page — site changed?")
        return m.group(1)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(DESHAW_DATA.format(build=self.build_id()))
        rows = ((data.get("pageProps") or {}).get("regularJobs"))
        if rows is None:
            raise AdapterError(f"{self.company}: payload has no pageProps.regularJobs")
        return FetchResult([self._posting(r) for r in rows])

    def _posting(self, raw):
        data = raw.get("data") or {}
        meta = data.get("jobMetadata") or {}
        places = [l.get("name") for l in (meta.get("jobLocations") or raw.get("office") or [])
                  if isinstance(l, dict)]
        jid = str(raw.get("id") or data.get("id") or "")
        slug = data.get("jobUrl") or jid
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=jid,
            title=(data.get("displayName") or raw.get("displayName") or "").strip(),
            location=join_locations(places),
            remote=False,
            posted_at="",                      # the payload publishes no posting date
            url=f"https://www.deshawindia.com/careers/{slug}",
            raw={k: v for k, v in raw.items() if k != "data"},
            req_id=jid,
        )
