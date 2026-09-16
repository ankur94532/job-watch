"""Render the store to static files for the read-only phone view.

Inert output only: `jobs.json` plus a page that reads it. There is no API, no
endpoint and no write path — the only writer is this laptop pushing a commit.

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
MAX_BYTES = 1_000_000          # shard by company above this, so mobile isn't downloading MBs

# Exactly the fields the page renders. Anything not listed here cannot leak.
FIELDS = ("company", "title", "location", "url", "req_id", "first_seen", "last_seen", "ats")


def collect(cfg, store, now=None):
    """Matching, currently-open rows. Closed reqs and non-matches are left behind."""
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


def payload(jobs, companies, now=None, polled_at=None):
    body = {
        "generated_at": int(now or time.time()),
        "companies": companies,
        "count": len(jobs),
        "jobs": jobs,
    }
    if polled_at:
        body["polled_at"] = int(polled_at)
    return body


def write(cfg, store, out_dir, now=None, max_bytes=MAX_BYTES):
    """Write the site and its data. Returns (list of paths written, payload bytes)."""
    now = int(now or time.time())
    jobs = collect(cfg, store, now)
    active, _ = config_mod.active_companies(cfg)
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    body = payload(jobs, len(active), now, polled_at=last_poll(store, [n for n, _ in active]))
    blob = dumps(body)
    written = []

    if len(blob) <= max_bytes:
        # One file: the page loads it and is done.
        for stale in os.listdir(data_dir):
            if stale != "jobs.json":
                os.remove(os.path.join(data_dir, stale))
        written.append(write_file(os.path.join(data_dir, "jobs.json"), blob))
    else:
        by_company = {}
        for j in jobs:
            by_company.setdefault(j["company"], []).append(j)
        shards = []
        for company, rows in sorted(by_company.items()):
            name = f"{safe_name(company)}.json"
            written.append(write_file(os.path.join(data_dir, name), dumps({"jobs": rows})))
            shards.append({"company": company, "file": name, "count": len(rows)})
        index = dict(body, jobs=[], shards=shards)
        blob = dumps(index)
        written.append(write_file(os.path.join(data_dir, "jobs.json"), blob))
        keep = {"jobs.json"} | {s["file"] for s in shards}
        for stale in os.listdir(data_dir):
            if stale not in keep:
                os.remove(os.path.join(data_dir, stale))
        log.info("payload over %d bytes, sharded into %d company files", max_bytes, len(shards))

    for name in SITE_FILES:
        dst = os.path.join(out_dir, name)
        shutil.copyfile(os.path.join(SITE_DIR, name), dst)
        written.append(dst)
    log.info("exported %d open matching reqs to %s", len(jobs), out_dir)
    return written, blob


def dumps(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def write_file(path, blob):
    with open(path, "wb") as f:
        f.write(blob)
    return path


def safe_name(company):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in company.lower())
