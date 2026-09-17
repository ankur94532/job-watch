"""Render the store to static files for the read-only phone view.

Inert output only: an index plus one file per company, and a page that reads
them. There is no API, no endpoint and no write path — the only writer is this
laptop pushing a commit.

Layout:

    data/jobs.json        index: meta + one row per company (name, count, newest)
    data/<company>.json   that company's open matching reqs, newest poll first

Sharding is unconditional rather than a size fallback: the home page is a list
of companies, so it needs only the index (a couple of KB), and opening one
company fetches only that company. The alternative — one 270KB blob — is
downloaded in full on a phone to read twenty rows.

What goes in: matching, currently-open requisitions, and enough per row to read
and act on one. What never goes in: config, tokens, backoff state, error text,
`last_error`, or the raw ATS payloads.
"""

import json
import logging
import os
import shutil
import time

from . import config as config_mod
from .filters import filter_for

log = logging.getLogger(__name__)

SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
SITE_FILES = ("index.html", "app.css", "app.js")
INDEX_FILE = "jobs.json"

# Exactly the fields the page renders. Anything not listed here cannot leak.
# `job_id` is the store's own key for the posting: it is what the page ticks off
# as "dealt with", and it is the only identifier every ATS supplies — 1,364 rows
# across Ashby, Greenhouse, Lever and Rippling carry no requisition number at all.
FIELDS = ("company", "job_id", "title", "location", "url", "req_id", "first_seen", "last_seen", "ats")


def collect(cfg, store, now=None):
    """Matching, currently-open rows, newest poll first."""
    now = int(now or time.time())
    defaults = cfg.get("defaults") or {}
    filters, jobs = {}, []
    for row in store.rows():
        company = row["company"]
        # A row is closed when it is older than its company's last complete sweep.
        if row["last_complete"] is not None and row["last_seen"] < row["last_complete"]:
            continue
        ccfg = cfg["companies"].get(company) or {}
        flt = filters.setdefault(company, filter_for(ccfg, defaults))
        if not (flt.title_ok(row["title"]) and flt.location_ok(row["location"])):
            continue
        jobs.append({k: row[k] for k in FIELDS})
    jobs.sort(key=lambda j: (-(j["first_seen"] or 0), j["company"]))
    return jobs


def last_poll(store, companies):
    """Newest successful poll across watched companies — the data's real age.

    Not the same as `generated_at`: rendering the page does not refresh the
    store. Exporting after a laptop has been shut all night would otherwise
    publish yesterday's jobs stamped "updated just now", which is the exact
    failure a freshness indicator exists to prevent.
    """
    stamps = []
    for name in companies:
        st = store.state(name)
        when = st.get("last_complete") or st.get("last_polled")
        if when:
            stamps.append(when)
    return max(stamps) if stamps else None


def group(jobs):
    """{company: [jobs, newest poll first]}, preserving collect()'s ordering."""
    by_company = {}
    for j in jobs:
        by_company.setdefault(j["company"], []).append(j)
    return by_company


def index_payload(jobs, watched, now, polled_at=None):
    companies = []
    for company, rows in group(jobs).items():
        companies.append({
            "company": company,
            "count": len(rows),
            # Newest poll that turned something up here: what the home page sorts
            # and badges on, without fetching a single company file.
            "newest": max(r["first_seen"] or 0 for r in rows),
            "file": shard_name(company),
        })
    companies.sort(key=lambda c: (-c["newest"], c["company"]))
    body = {
        "generated_at": int(now),
        "watched": watched,
        "count": len(jobs),
        "companies": companies,
    }
    if polled_at:
        body["polled_at"] = int(polled_at)
    return body


def write(cfg, store, out_dir, now=None):
    """Write the site and its data. Returns (paths written, index bytes)."""
    now = int(now or time.time())
    jobs = collect(cfg, store, now)
    active, _ = config_mod.active_companies(cfg)
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    body = index_payload(jobs, len(active), now, polled_at=last_poll(store, [n for n, _ in active]))
    blob = dumps(body)
    written = [write_file(os.path.join(data_dir, INDEX_FILE), blob)]

    by_company = group(jobs)
    keep = {INDEX_FILE}
    for company, rows in by_company.items():
        name = shard_name(company)
        keep.add(name)
        written.append(write_file(os.path.join(data_dir, name),
                                  dumps({"company": company, "count": len(rows), "jobs": rows})))
    for stale in os.listdir(data_dir):     # a company that stops matching leaves no orphan file
        if stale not in keep:
            os.remove(os.path.join(data_dir, stale))

    for name in SITE_FILES:
        dst = os.path.join(out_dir, name)
        shutil.copyfile(os.path.join(SITE_DIR, name), dst)
        written.append(dst)
    log.info("exported %d open matching reqs across %d companies to %s",
             len(jobs), len(by_company), out_dir)
    return written, blob


def dumps(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def write_file(path, blob):
    with open(path, "wb") as f:
        f.write(blob)
    return path


def shard_name(company):
    return safe_name(company) + ".json"


def safe_name(company):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in company.lower())
