"""Goldman Sachs "Higher" (higher.gs.com): their own careers platform.

POST https://api-higher.gs.com/gateway/api/v1/graphql, operation GetRoles, with
no login or session: `roleSearch` takes a page (pageSize up to 200; 500 is
refused) and returns `totalCount` plus the roles. Checked 2026-09-18: 962
roles, about a fifth in India.

robots.txt: higher.gs.com allows only /roles/ (the listing page /results is
disallowed); the API is a separate host, api-higher.gs.com, with no robots.txt.

Job links: https://higher.gs.com/roles/<externalSource.sourceId>. Verified in a
browser, because the page is client-rendered and answers 200 for ANY id: the
sourceId form shows the role, while the roleId form ("184745_GS_MID_CAREER")
and a made-up id both render "Oops, something went wrong". A 200 is not proof.

The sort is relevance, with no search term, so pages are deduplicated and the
total is held against `totalCount`: a short sweep raises instead of retiring
roles that were merely not returned.
"""

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

API = "https://api-higher.gs.com/gateway/api/v1/graphql"
ROLE_URL = "https://higher.gs.com/roles/{}"
PAGE = 100
MAX_PAGES = 30
MIN_COVERAGE = 0.95

QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      jobTitle
      corporateTitle
      division
      locations { city state country }
      externalSource { sourceId }
    }
  }
}"""


class GoldmanAdapter(Adapter):
    ats = "gshigher"
    required = ()
    paginated = True

    def fetch_page(self, n):
        body = {
            "operationName": "GetRoles",
            "query": QUERY,
            "variables": {"searchQueryInput": {
                "page": {"pageSize": PAGE, "pageNumber": n},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "filters": [],
                "experiences": ["EARLY_CAREER", "PROFESSIONAL"],
                "searchTerm": "",
            }},
        }
        data = self.http.post_json(API, body, headers={"Origin": "https://higher.gs.com",
                                                      "Referer": "https://higher.gs.com/"})
        search = (data or {}).get("data", {}) and data["data"].get("roleSearch")
        if not search:
            raise AdapterError(f"{self.company}: roleSearch returned nothing: {str(data.get('errors'))[:200]}")
        return search

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        out, seen, total = [], set(), None
        for n in range(MAX_PAGES):
            if n:
                self.pause()
            search = self.fetch_page(n)
            total = search.get("totalCount") if total is None else total
            items = search.get("items") or []
            for raw in items:
                p = self._posting(raw)
                if p and p.job_id not in seen:
                    seen.add(p.job_id)
                    out.append(p)
            if len(items) < PAGE or (total and len(seen) >= total):
                break
        if total and len(seen) < total * MIN_COVERAGE:
            raise AdapterError(f"{self.company}: got {len(seen)} of {total} roles — incomplete sweep, not trusting it")
        return FetchResult(out)

    def _posting(self, raw):
        source = (raw.get("externalSource") or {}).get("sourceId")
        if not source:
            return None
        places = [", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
                  for loc in raw.get("locations") or []]
        location = join_locations(places)
        return Posting(
            company=self.company, ats=self.ats, job_id=str(source),
            title=(raw.get("jobTitle") or "").strip(), location=location,
            remote="remote" in location.lower(), posted_at="",
            url=ROLE_URL.format(source), raw=raw, req_id=str(source),
        )
