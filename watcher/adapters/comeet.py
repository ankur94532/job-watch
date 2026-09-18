"""Comeet (`comeet.co/careers-api/2.0`), as embedded by its WordPress plugin.

The ScyllaDB careers page renders its openings server-side through the Comeet WP
plugin, so an earlier probe wrote it off as un-pollable. It is not: the plugin
prints the board's own credentials into the page, and the public API answers
with them.

    GET https://www.comeet.co/careers-api/2.0/company/E4.006/positions?token=<token>
    -> 200, a bare JSON list of positions

**The token is not a secret.** It is a read-only board identifier that Comeet
publishes in the markup of every customer's public careers page — the same role
a Greenhouse board token plays. It grants nothing but the listing that page
already shows. It still lives in config and never in the export, per the
"nothing in the export that would matter if it leaked" rule.

Per row: `uid` (the permalink segment), `name`, `department`,
`location` {name, city, country, is_remote}, `time_updated`, and
`url_comeet_hosted_page`, which is a real permalink, so none is synthesised.

One trap: a position open in several countries appears once per location with
uids like `FC.078` and `FC.078-75.10C`. Those are genuinely distinct
requisitions to apply to, so they are kept apart rather than merged.

Multi-tenant: another Comeet customer is a `uid` and `token` away — both
readable from their careers page markup.
"""

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

API = "https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}"


class ComeetAdapter(Adapter):
    ats = "comeet"
    required = ("company_uid", "token")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = API.format(uid=self.cfg["company_uid"], token=self.cfg["token"])
        rows = self.http.get_json(url, cache=ctx.cache)   # a 304 raises, and the runner catches it
        if not isinstance(rows, list):
            raise AdapterError(f"{self.company}: expected a list of positions, got "
                               f"{type(rows).__name__} — token revoked or API changed?")
        return FetchResult([self._posting(r) for r in rows])

    def _posting(self, raw):
        loc = raw.get("location") or {}
        places = [loc.get("name")] if loc.get("name") else [loc.get("city"), loc.get("country")]
        uid = str(raw.get("uid") or "")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=uid,
            title=(raw.get("name") or "").strip(),
            location=join_locations([p for p in places if p]),
            remote=bool(loc.get("is_remote")),
            posted_at=(raw.get("time_updated") or "")[:10],
            url=raw.get("url_comeet_hosted_page") or raw.get("url_active_page") or "",
            raw={k: v for k, v in raw.items() if k not in ("details", "description")},
            req_id=uid,
        )
