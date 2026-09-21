"""IBM's first-party careers search service, country-filtered in one request."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

API = "https://www-api.ibm.com/search/api/v1/ibmcom/appid/careers/responseFormat/json"
METADATA = "field_keyword_05,field_keyword_19,field_keyword_17,dcdate"
ROW_ID = re.compile(r"[0-9a-f]{64}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class IBMAdapter(RobotsAware):
    ats = "ibm"

    def url(self):
        return API + "?" + urlencode({
            "scope": "careers2",
            "rmdt": METADATA,
            "appid": "careers",
            "sortby": "-dcdate",
            "nr": 1000,
            "fr": 0,
            "page": 1,
            "query": "",
            "filter": "field_keyword_05:India",
        })

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = self.url()
        self.check_robots(url)
        data = self.http.get_json(url, cache=ctx.cache)
        result = data.get("resultset") if isinstance(data, dict) else None
        query = result.get("searchquery") if isinstance(result, dict) else None
        search = result.get("searchresults") if isinstance(result, dict) else None
        if (not isinstance(query, dict) or query.get("appid") != "careers"
                or query.get("collection") != "ibmcom"
                or query.get("userquery") != ""
                or query.get("filter") != "field_keyword_05:India"
                or not isinstance(query.get("sources"), str) or not query["sources"]
                or not isinstance(search, dict)
                or type(search.get("totalresults")) is not int
                or search["totalresults"] < 0
                or search.get("startindex") != 0
                or search.get("numresults") != search["totalresults"]
                or not isinstance(search.get("searchresultlist"), list)
                or len(search["searchresultlist"]) != search["totalresults"]):
            raise AdapterError(f"{self.company}: incomplete or mismatched IBM search result")
        rows = [self._posting(raw, number) for number, raw in
                enumerate(search["searchresultlist"])]
        ids = [row.job_id for row in rows]
        raw_ids = [row.raw["search_id"] for row in rows]
        if len(ids) != len(set(ids)) or len(raw_ids) != len(set(raw_ids)):
            raise AdapterError(f"{self.company}: duplicate IBM search identity")
        return FetchResult(rows)

    def _posting(self, raw, number):
        if not isinstance(raw, dict) or raw.get("resultnum") != number:
            raise AdapterError(f"{self.company}: invalid IBM result sequence")
        search_id, title, url = (raw.get(key) for key in ("id", "title", "url"))
        if (not isinstance(search_id, str) or not ROW_ID.fullmatch(search_id)
                or not isinstance(title, str) or not title.strip()
                or not isinstance(url, str)):
            raise AdapterError(f"{self.company}: IBM job is missing identity/title/url")
        attributes = raw.get("docattributes")
        if not isinstance(attributes, list) or any(not isinstance(item, dict)
                                                   for item in attributes):
            raise AdapterError(f"{self.company}: invalid IBM job metadata")

        def one(key):
            values = [item[key] for item in attributes if key in item]
            if len(values) != 1 or not isinstance(values[0], str):
                raise AdapterError(f"{self.company}: missing/duplicate IBM {key}")
            return values[0].strip()

        country = one("field_keyword_05")
        location = one("field_keyword_19")
        workstyle = one("field_keyword_17")
        posted = one("dcdate")
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        job_ids = query.get("jobId") or []
        if (country != "India" or not location or not DATE.fullmatch(posted)
                or parts.scheme != "https" or parts.netloc != "careers.ibm.com"
                or parts.path != "/careers/JobDetail" or set(query) != {"jobId"}
                or len(job_ids) != 1 or not job_ids[0].isdigit()):
            raise AdapterError(f"{self.company}: mismatched IBM country/location/job URL")
        job_id = job_ids[0]
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=title.strip(),
            location=join_locations(location, country),
            remote="remote" in workstyle.lower(),
            posted_at=posted,
            url=url,
            raw={"search_id": search_id, "country": country, "location": location,
                 "workstyle": workstyle},
            req_id=job_id,
        )
