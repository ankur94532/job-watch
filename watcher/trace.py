"""`watcher trace` — a DEV-ONLY discovery tool. Never used by the poller.

A client-rendered careers page fetches its listings over XHR after load, so
nothing in the served HTML or its bundles names the endpoint. This drives a real
browser once, records every JSON response, and prints a candidate adapter config:
URL, method, headers worth keeping, payload, and the path to the job array.

You review that, and the runtime adapter is written in plain stdlib like every
other one. Playwright is imported inside the function, so:

* the polling path never imports it, and launchd never touches it;
* it stays out of the runtime dependency set (`requirements.txt` is still empty);
* `watcher run` keeps working on a stock `/usr/bin/python3` with no browser.

Install it only where you run traces:

    python3 -m venv .venv-dev && .venv-dev/bin/pip install playwright
    .venv-dev/bin/playwright install chromium
    .venv-dev/bin/python -m watcher trace zepto --url https://www.zeptonow.com/careers

robots.txt is checked first and a disallowed page is refused, browser or not.
"""

import json
import re
import sys

from .robots import fetch as fetch_robots
from .http import user_agent

JOB_KEYS = ("title", "jobtitle", "name", "position", "positiontitle", "role", "designation")
LOCATION_KEYS = ("location", "city", "locations", "joblocation", "office", "workplace")


def _looks_like_jobs(node):
    """A list of dicts that each carry something title-shaped."""
    if not isinstance(node, list) or len(node) < 2:
        return 0
    rows = [r for r in node[:5] if isinstance(r, dict)]
    if len(rows) < 2:
        return 0
    keys = {k.lower() for r in rows for k in r}
    if not keys & set(JOB_KEYS):
        return 0
    score = len(node)
    if keys & set(LOCATION_KEYS):
        score *= 2                      # title AND location: almost certainly the list
    return score


def find_job_array(payload):
    """-> (json path, rows) for the most job-shaped array in the document."""
    best = (None, None, 0)

    def walk(node, path="$"):
        nonlocal best
        score = _looks_like_jobs(node)
        if score > best[2]:
            best = (path, node, score)
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node[:50]):
                walk(v, f"{path}[{i}]")

    walk(payload)
    return best[0], best[1]


def trace(url, company, timeout=45000, interact=True, out=sys.stdout):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed, and deliberately is not a runtime dependency.\n"
            "  python3 -m venv .venv-dev && .venv-dev/bin/pip install playwright\n"
            "  .venv-dev/bin/playwright install chromium\n"
            "  .venv-dev/bin/python -m watcher trace <company> --url <careers page>")

    robots = fetch_robots(url)
    if not robots.allowed(user_agent(), url):
        raise SystemExit(f"robots.txt forbids {url} ({robots.why(user_agent(), url)}) — not tracing")

    seen = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(user_agent=user_agent())
        page = ctx.new_page()

        def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = resp.json()
            except Exception:
                return
            path, rows = find_job_array(body)
            if not rows:
                return
            req = resp.request
            seen.append({
                "url": resp.url,
                "method": req.method,
                "status": resp.status,
                "post_data": req.post_data,
                "headers": {k: v for k, v in (req.headers or {}).items()
                            if k.lower() in ("content-type", "accept", "x-api-key", "authorization",
                                             "x-requested-with", "origin", "referer")},
                "json_path": path,
                "count": len(rows),
                "sample": rows[0],
            })

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=timeout)

        if interact:
            # Pagination and filters often reveal the real endpoint and its params.
            for pattern in (r"load more", r"show more", r"next", r"view all", r"see all"):
                for btn in page.get_by_role("button", name=re.compile(pattern, re.I)).all()[:2]:
                    try:
                        btn.click(timeout=3000)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
            for _ in range(3):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)

        browser.close()

    if not seen:
        print(f"no JSON response carrying a job array was seen on {url}", file=out)
        print("the page may render server-side, or fetch its listings from another origin", file=out)
        return 1

    seen.sort(key=lambda h: -h["count"])
    print(f"# {len(seen)} candidate endpoint(s) for {company}, most rows first\n", file=out)
    for hit in seen[:5]:
        print(f"## {hit['method']} {hit['url']}", file=out)
        print(f"   status={hit['status']} rows={hit['count']} json_path={hit['json_path']}", file=out)
        if hit["headers"]:
            print(f"   headers: {json.dumps(hit['headers'])}", file=out)
        if hit["post_data"]:
            print(f"   payload: {hit['post_data'][:400]}", file=out)
        sample = {k: v for k, v in list(hit["sample"].items())[:10]
                  if not isinstance(v, (dict, list))}
        print(f"   sample row: {json.dumps(sample, default=str)[:400]}", file=out)
        print(file=out)

    best = seen[0]
    print("# candidate config — review before committing, and verify the brand and", file=out)
    print("# locations really are the company you want (see the ashby/navi trap)", file=out)
    print(f"  {company}:", file=out)
    print("    ats: <write a stdlib adapter for this shape>", file=out)
    print(f"    url: {best['url']}", file=out)
    print(f"    method: {best['method']}", file=out)
    print(f"    json_path: {best['json_path']}", file=out)
    return 0
