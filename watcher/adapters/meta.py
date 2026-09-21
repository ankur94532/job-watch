"""Meta's public, anonymous careers search.

Meta's detail response and JobPosting JSON-LD occasionally omit locations.
The careers UI's own search operation does not have that gap: it accepts the
current office values from its public location-filter operation and returns
the complete filtered result set without pagination.  Fetch those values at
runtime so office additions and renames are not hardcoded.

The persisted query IDs were live-validated on 2026-09-19.  They can change
when Meta deploys; a stale ID fails loudly through the response-shape checks.
"""

import json
import re
from urllib.parse import urlencode

from .base import AdapterError, FetchResult, Posting, join_locations
from .embedded import RobotsAware

PAGE = "https://www.metacareers.com/jobsearch/"
GRAPHQL = "https://www.metacareers.com/api/graphql/"
SEARCH_OPERATION = "CareersJobSearchResultsDataQuery"
SEARCH_DOC_ID = "27506805582236862"
LOCATION_OPERATION = "CareersJobSearchLocationFilterV3Query"
LOCATION_DOC_ID = "24867916029505828"

_LSD = re.compile(r'\["LSD",\[\],(\{.*?\}),\d+\]', re.S)
_SITE = re.compile(r'\["SiteData",\[\],(\{.*?\}),\d+\]', re.S)


class MetaAdapter(RobotsAware):
    ats = "meta"
    paginated = True

    def fetch(self, ctx=None):
        self.check_robots(PAGE)
        self.check_robots(GRAPHQL)
        page = self.http.get_text(PAGE)
        lsd_match, site_match = _LSD.search(page), _SITE.search(page)
        if not lsd_match or not site_match:
            raise AdapterError(f"{self.company}: Meta search page lacks request context")
        try:
            lsd = json.loads(lsd_match.group(1))["token"]
            site = json.loads(site_match.group(1))
            if not isinstance(lsd, str) or not lsd:
                raise ValueError
            for key in ("haste_session", "client_revision", "__spin_r", "__spin_b",
                        "__spin_t", "hsi"):
                if key not in site:
                    raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise AdapterError(f"{self.company}: invalid Meta request context") from None

        self.pause()
        filters = self._query(LOCATION_OPERATION, LOCATION_DOC_ID, {}, lsd, site)
        location_node = (filters.get("data") or {}).get("job_search_filters")
        locations = location_node.get("locations") if isinstance(location_node, dict) else None
        if not isinstance(locations, list):
            raise AdapterError(f"{self.company}: invalid Meta location-filter response")
        country = self.cfg.get("country") or "India"
        offices = []
        for location in locations:
            if not isinstance(location, dict):
                raise AdapterError(f"{self.company}: invalid Meta location filter")
            if location.get("country") == country:
                name = location.get("location_display_name")
                if not isinstance(name, str) or not name.strip():
                    raise AdapterError(f"{self.company}: invalid Meta {country} office")
                offices.append(name.strip())
        if not offices or len(offices) != len(set(offices)):
            raise AdapterError(f"{self.company}: empty/duplicate Meta {country} offices")

        search_input = {
            "q": None,
            "divisions": [],
            "offices": offices,
            "roles": [],
            "leadership_levels": [],
            "saved_jobs": [],
            "saved_searches": [],
            "sub_teams": [],
            "teams": [],
            "is_leadership": False,
            "is_remote_only": False,
            "sort_by_new": False,
            "page": 1,
            "results_per_page": None,
        }
        self.pause()
        payload = self._query(SEARCH_OPERATION, SEARCH_DOC_ID, {
            "isLoggedIn": False,
            "search_input": search_input,
            "viewasUserID": None,
        }, lsd, site)
        search = (payload.get("data") or {}).get("job_search_with_featured_jobs")
        rows = search.get("all_jobs") if isinstance(search, dict) else None
        featured = search.get("featured_jobs") if isinstance(search, dict) else None
        if not isinstance(rows, list) or not isinstance(featured, list):
            raise AdapterError(f"{self.company}: invalid Meta job-search response")
        postings = [self._posting(row, country) for row in rows]
        ids = [posting.job_id for posting in postings]
        if len(ids) != len(set(ids)):
            raise AdapterError(f"{self.company}: duplicate Meta job identity")
        seen = set(ids)
        for row in featured:
            posting = self._posting(row, country, filtered_result=False)
            # Featured promotions are global even when all_jobs is filtered by
            # office. Include only jobs whose own display locations establish
            # country membership; a locationless promotion proves nothing.
            locations = row.get("locations")
            if (any(country.lower() in location.lower() for location in locations)
                    and posting.job_id not in seen):
                postings.append(posting)
                seen.add(posting.job_id)
        return FetchResult(postings)

    def _query(self, operation, doc_id, variables, lsd, site):
        form = {
            "__a": "1",
            "__user": "0",
            "__req": "1",
            "__hs": site["haste_session"],
            "__rev": str(site["client_revision"]),
            "__spin_r": str(site["__spin_r"]),
            "__spin_b": str(site["__spin_b"]),
            "__spin_t": str(site["__spin_t"]),
            "__hsi": str(site["hsi"]),
            "lsd": lsd,
            "jazoest": "2" + str(sum(ord(char) for char in lsd)),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": operation,
            "server_timestamps": "true",
            "doc_id": doc_id,
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        body, _ = self.http.request(
            "POST", GRAPHQL, data=urlencode(form).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-FB-LSD": lsd,
                "X-FB-Friendly-Name": operation,
                "Origin": "https://www.metacareers.com",
                "Referer": PAGE,
            })
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise AdapterError(f"{self.company}: Meta {operation} returned invalid JSON") from None
        if not isinstance(payload, dict) or payload.get("errors"):
            raise AdapterError(f"{self.company}: Meta {operation} returned errors")
        return payload

    def _posting(self, row, country, filtered_result=True):
        if not isinstance(row, dict):
            raise AdapterError(f"{self.company}: invalid Meta job")
        job_id, title, locations = row.get("id"), row.get("title"), row.get("locations")
        if (not isinstance(job_id, str) or not job_id.isdigit()
                or not isinstance(title, str) or not title.strip()
                or not isinstance(locations, list)
                or any(not isinstance(location, str) or not location.strip()
                       for location in locations)):
            raise AdapterError(f"{self.company}: malformed Meta job")
        # Membership in all_jobs was filtered by Meta against every current
        # office whose country is India. If Meta omits display locations for a
        # matched row, retain the established country without inventing a city.
        # Global featured promotions do not carry that guarantee.
        if (filtered_result and locations
                and not any(country.lower() in location.lower() for location in locations)):
            raise AdapterError(f"{self.company}: Meta returned a job outside {country} filter")
        location = join_locations(locations) or (country if filtered_result else "")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=title.strip(),
            location=location,
            remote=any("remote" in value.lower() for value in locations),
            posted_at="",
            url=f"https://www.metacareers.com/profile/job_details/{job_id}/",
            raw=row,
            req_id=job_id,
        )
