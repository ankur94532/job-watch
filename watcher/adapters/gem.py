"""Gem job boards (`jobs.gem.com/<board>`), found by tracing PromptQL/Hasura.

The board is a JS app: the served HTML carries no titles and no endpoint. A
trace caught one POST, which needs no auth, no cookie and no token — only the
board id, which is the path segment of the public board URL:

    POST https://jobs.gem.com/api/public/graphql/batch
    content-type: application/json
    [{"operationName":"JobBoardList","variables":{"boardId":"promptql"},"query":"..."}]

Verified 2026-09-18 against `promptql` (Hasura's board after the rebrand).
Rows arrive at `[0].data.oatsExternalJobPostings.jobPostings`, each with `id`,
`extId`, `title`, `locations[]` (name, city, isoCountry, isRemote) and
`job.department`. `extId` is the permalink segment: `jobs.gem.com/<board>/<extId>`
is a real page (and server-renders the title into `<title>`), while the opaque
`id` is only good for dedup.

Multi-tenant: another Gem customer is one `board` value away. The query is sent
verbatim rather than trimmed — a shortened query is a different request to their
server, and this one is known to work.
"""

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

API = "https://jobs.gem.com/api/public/graphql/batch"
QUERY = (
    "query JobBoardList($boardId: String!) {\n"
    "  oatsExternalJobPostings(boardId: $boardId) {\n"
    "    jobPostings {\n      id\n      extId\n      title\n"
    "      locations {\n        id\n        name\n        city\n        isoCountry\n"
    "        isRemote\n        extId\n        __typename\n      }\n"
    "      job {\n        id\n        department {\n          id\n          name\n"
    "          extId\n          __typename\n        }\n        locationType\n"
    "        employmentType\n        __typename\n      }\n      __typename\n    }\n"
    "    __typename\n  }\n}\n"
)


class GemAdapter(Adapter):
    ats = "gem"
    required = ("board",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        payload = [{"operationName": "JobBoardList",
                    "variables": {"boardId": self.cfg["board"]},
                    "query": QUERY}]
        data = self.http.post_json(API, payload, headers={
            "Accept": "*/*",
            "Referer": f"https://jobs.gem.com/{self.cfg['board']}",
        })
        if not isinstance(data, list) or not data:
            raise AdapterError(f"{self.company}: batch response was not a list — shape changed?")
        postings = (((data[0].get("data") or {}).get("oatsExternalJobPostings") or {})
                    .get("jobPostings"))
        if postings is None:
            raise AdapterError(f"{self.company}: no oatsExternalJobPostings.jobPostings in response")
        return FetchResult([self._posting(r) for r in postings])

    def _posting(self, raw):
        locs = raw.get("locations") or []
        places = [l.get("name") or l.get("city") for l in locs if isinstance(l, dict)]
        job = raw.get("job") or {}
        jid = str(raw.get("id") or raw.get("extId") or "")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=jid,
            title=(raw.get("title") or "").strip(),
            location=join_locations([p for p in places if p]),
            remote=any(l.get("isRemote") for l in locs if isinstance(l, dict))
            or "remote" in (job.get("locationType") or "").lower(),
            posted_at="",                  # the board publishes no posting date
            url=f"https://jobs.gem.com/{self.cfg['board']}/{raw.get('extId') or jid}",
            raw=raw,
            req_id=str(raw.get("extId") or ""),
        )
