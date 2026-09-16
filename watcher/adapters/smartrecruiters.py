from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting

API = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={limit}&offset={offset}"
VIEW = "https://jobs.smartrecruiters.com/{token}/{id}"

PAGE = 100          # API maximum
MAX_PAGES = 100


class SmartRecruitersAdapter(Adapter):
    ats = "smartrecruiters"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        token = self.cfg["token"]
        out, seen = [], set()
        offset, pages, total = 0, 0, None
        # Page 0 honours If-None-Match (verified). Only trust a 304 when the whole
        # board fitted on that page last time; otherwise later pages could differ.
        validators = dict(ctx.cache) if ctx.cache.get("single_page") else {}
        while True:
            if pages >= MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {len(out)}/{total}")
            if pages:
                self.pause()
            url = API.format(token=token, limit=PAGE, offset=offset)
            data = self.http.get_json(url, cache=validators) if pages == 0 else self.http.get_json(url)
            if total is None:
                total = data.get("totalFound", 0)
            content = data.get("content") or []
            pages += 1
            for p in content:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                out.append(self._posting(token, p))
            offset += len(content)
            if not content or offset >= total:
                break
        ctx.cache.clear()
        if pages == 1:
            ctx.cache.update(validators, single_page=True)
        return FetchResult(out)

    def _posting(self, token, p):
        loc = p.get("location") or {}
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=str(p["id"]),
            title=(p.get("name") or "").strip(),
            location=loc.get("fullLocation")
            or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
            remote=bool(loc.get("remote")),
            posted_at=p.get("releasedDate") or "",
            url=VIEW.format(token=token, id=p["id"]),
            raw=p,
            req_id=p.get("refNumber") or "",
        )
