"""Monthly look at companies that have no board today: has one appeared?

NOT part of polling. `watcher recheck` is run by its own launchd job on the
1st of the month (deploy/com.shashwattripathi.jobswatch.recheck.plist), makes
a handful of requests, and only reports. It never enables a company: a board
that appears still needs an adapter chosen and its filters checked by a person.

Each company with a `recheck:` list in config.yaml is checked at those URLs
(its old careers page, and the ATS APIs it would most likely come back on).
A URL is reduced to a fingerprint:

    status     HTTP status (a 404 turning into a 200 is the usual first sign)
    items      for a JSON answer, how many jobs it lists
    ats        which ATS hosts the page links to (greenhouse.io, lever.co, ...)
    joblinks   how many links on the page look like job or openings pages

and compared with the fingerprint from the previous check, stored in the
database. The first check only records a baseline. Anything that moved is
reported, and sent as one message if a notifier is configured.
"""

import json
import logging
import re
import time

from .http import HttpStatusError, NetworkError

log = logging.getLogger(__name__)

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com",
             "workable.com", "darwinbox.in", "keka.com", "freshteam.com", "zohorecruit", "recruitee.com",
             "bamboohr.com", "skima.ai", "rippling.com", "jobs.gem.com", "turbohire", "eightfold.ai",
             "icims.com", "jobvite.com", "teamtailor.com", "join.com")
JOB_LINK = re.compile(r'href="[^"]*/(?:jobs?|openings?|positions?|careers?)/[^"#?]+"', re.I)
JSON_LISTS = ("jobs", "jobPostings", "content", "postings", "results", "data")


def fingerprint(http, url):
    """What a URL looks like right now, or None if it could not be reached."""
    try:
        body, _ = http.request("GET", url, headers={"Accept": "application/json, text/html;q=0.9"})
        status = 200
    except HttpStatusError as e:
        return {"status": e.code, "items": None, "ats": [], "joblinks": 0}
    except NetworkError:
        return None
    text = body.decode("utf-8", errors="replace")
    items = None
    try:
        data = json.loads(text)
        if isinstance(data, list):
            items = len(data)
        elif isinstance(data, dict):
            items = next((len(data[k]) for k in JSON_LISTS if isinstance(data.get(k), list)), None)
    except ValueError:
        pass
    ats = sorted({h for h in ATS_HOSTS if h in text})
    return {"status": status, "items": items, "ats": ats, "joblinks": len(JOB_LINK.findall(text))}


def describe(old, new):
    """None if nothing moved, else (appeared?, what changed)."""
    if old == new:
        return None
    bits, appeared = [], False
    if old["status"] != new["status"]:
        bits.append(f"HTTP {old['status']} -> {new['status']}")
        appeared |= old["status"] >= 400 and new["status"] < 400
    if (old.get("items") or 0) != (new.get("items") or 0):
        bits.append(f"{old.get('items') or 0} -> {new.get('items') or 0} jobs listed")
        appeared |= not old.get("items") and bool(new.get("items"))
    added = sorted(set(new["ats"]) - set(old["ats"]))
    if added:
        bits.append("now links to " + ", ".join(added))
        appeared = True
    if set(old["ats"]) - set(new["ats"]):
        bits.append("no longer links to " + ", ".join(sorted(set(old["ats"]) - set(new["ats"]))))
    if old["joblinks"] != new["joblinks"]:
        bits.append(f"{old['joblinks']} -> {new['joblinks']} job-like links")
        appeared |= old["joblinks"] == 0 and new["joblinks"] > 0
    return appeared, "; ".join(bits)


def run(cfg, store, http, sink=None, now=None, sleep=time.sleep):
    """Check every `recheck:` URL. Returns report lines; notifies if anything moved."""
    now = int(now or time.time())
    lines, moved = [], []
    targets = [(name, url) for name, c in cfg["companies"].items() for url in (c.get("recheck") or [])]
    for i, (company, url) in enumerate(targets):
        if i:
            sleep(1.0)
        new = fingerprint(http, url)
        if new is None:
            lines.append(f"{company:10} unreachable   {url}")
            continue
        old = store.recheck_get(url)
        store.recheck_put(url, company, new, now)
        if old is None:
            lines.append(f"{company:10} baseline      {url}  (HTTP {new['status']})")
            continue
        change = describe(old, new)
        if change is None:
            lines.append(f"{company:10} unchanged     {url}  (HTTP {new['status']})")
            continue
        appeared, what = change
        tag = "APPEARED?" if appeared else "changed"
        lines.append(f"{company:10} {tag:13} {url}  {what}")
        moved.append(f"{company}: {tag} — {what}\n{url}")
    for line in lines:
        log.info("recheck: %s", line)
    if moved and sink is not None:
        sink.send("Board re-check: something moved. Look before enabling anything.\n\n" + "\n\n".join(moved))
    return lines
