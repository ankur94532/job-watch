"""Two devices, one person: drive tick sync in a real browser. DEV-ONLY.

    .venv-dev/bin/python devtools/sync_check.py

Starts devtools/pages_dev.mjs (docs/ plus the real sync function behind a fake
Access), opens a "laptop" and a "phone" as separate browser contexts, so each
has its own localStorage exactly like two devices, and checks that what one
does shows up on the other. Run `python -m watcher export` first so docs/ holds
the current page.
"""

import base64
import http.server
import json
import os
import socketserver
import threading
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8792
BASE = "http://127.0.0.1:%d/" % PORT
fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + detail if detail else ""))
    if not cond:
        fails.append(name)


def poke(path):
    return urllib.request.urlopen(BASE + path).read().decode()


def of(p):
    """The whole filtered list's length, from 'showing X of N'."""
    return int(re.search(r"of (\d+)", p.inner_text("#summary")).group(1))


def open_company(p, name):
    """Via home, as a person would: re-opening the screen you are already on
    is not a navigation, and the list deliberately does not move under you."""
    p.goto(BASE + "index.html#/", wait_until="networkidle")
    p.wait_for_selector(".company-row")
    p.goto(BASE + "index.html#/c/" + name, wait_until="networkidle")
    p.wait_for_selector(".job .tick")


def static_server(port):
    """docs/ with no API at all, like the laptop's local copy on 127.0.0.1:8765."""
    root = os.path.join(HERE, "..", "docs")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=root, **k)

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    from playwright.sync_api import sync_playwright

    server = subprocess.Popen(["node", os.path.join(HERE, "pages_dev.mjs"), str(PORT)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                poke("__state")
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            laptop = b.new_context(viewport={"width": 1000, "height": 800}).new_page()
            phone = b.new_context(viewport={"width": 390, "height": 720}).new_page()
            for p in (laptop, phone):
                p.on("dialog", lambda d: d.accept())
                p.goto(BASE + "index.html", wait_until="networkidle")
                p.wait_for_selector(".company-row")
            company = laptop.eval_on_selector(".company-row .company-name", "e => e.textContent")

            laptop.wait_for_timeout(1500)
            check("the page says it is synced", "synced" in laptop.inner_text("#freshness"),
                  laptop.inner_text("#freshness"))

            # ---- a tick on the laptop reaches the phone --------------------
            open_company(laptop, company)
            total = of(laptop)
            title = laptop.locator(".job .title").first.inner_text()
            laptop.locator(".job .tick").first.click()
            laptop.wait_for_timeout(2000)                    # debounce + round trip
            stored = json.loads(poke("__state"))
            check("the tick reached the server", any('"ticks"' in v and "[1," in v for v in stored.values()))

            open_company(phone, company)
            phone.wait_for_timeout(1500)                     # initial sync, then re-render
            check("the phone hides the job ticked on the laptop", of(phone) == total - 1,
                  "%d -> %d" % (total, of(phone)))

            # ---- an untick on the phone reaches the laptop, and stays -------
            phone.check("#showhidden")
            phone.wait_for_timeout(300)
            row = phone.locator(".job.is-ticked").first
            check("the phone shows it as ticked", row.count() == 1 and row.locator(".title").inner_text() == title)
            row.locator(".tick").click()
            phone.wait_for_timeout(2000)
            open_company(laptop, company)
            laptop.wait_for_timeout(1500)
            check("the laptop gets the untick back (not resurrected)", of(laptop) == total,
                  "%d of %d" % (of(laptop), total))

            # ---- opening a company on one device clears its dot on the other --
            for p in (laptop, phone):
                p.evaluate("""() => { const v = JSON.parse(localStorage.getItem('jobwatch:visited') || '{}');
                    Object.keys(v).forEach(k => v[k] = 1);          // everything new again
                    localStorage.setItem('jobwatch:visited', JSON.stringify(v)); }""")
            poke("__signin")
            laptop.goto(BASE + "index.html#/c/" + company, wait_until="networkidle")
            laptop.wait_for_timeout(2000)
            phone.goto(BASE + "index.html", wait_until="networkidle")
            phone.wait_for_selector(".company-row")
            phone.wait_for_timeout(1500)
            dot = phone.evaluate("""(nm) => [...document.querySelectorAll('.company-row')]
                .find(e => e.querySelector('.company-name').textContent === nm).querySelector('.dot').hidden""",
                                 company)
            check("opened on the laptop: no dot on the phone", dot is True, company)

            # ---- signed out: say so, do not pretend -------------------------
            poke("__signout")
            laptop.reload(wait_until="networkidle")
            laptop.wait_for_timeout(1500)
            check("an expired login is shown, not hidden",
                  "reload to sign in" in laptop.inner_text("#freshness"), laptop.inner_text("#freshness"))
            poke("__signin")

            # ---- the local copy's ticks carried over ------------------------
            payload = {"v": 1, "ticks": {company + "|carried-1": [1, 5]}, "opened": {}, "visited": {},
                       "seenThrough": 0}
            enc = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            phone.goto(BASE + "index.html#/import/" + enc, wait_until="networkidle")
            phone.wait_for_timeout(2000)
            check("import leaves no payload in the URL", phone.evaluate("location.hash") == "#/")
            check("imported ticks are stored and synced",
                  "carried-1" in poke("__state"))

            # ---- a copy with no endpoint offers to carry its ticks over -----
            local = static_server(PORT + 1)
            lp = b.new_context().new_page()
            lp.goto("http://127.0.0.1:%d/index.html" % (PORT + 1), wait_until="networkidle")
            lp.wait_for_selector(".company-row")
            check("no endpoint: no carry banner while there is nothing to carry",
                  lp.locator("#carry").is_hidden())
            lp.evaluate("localStorage.setItem('jobwatch:ticked', JSON.stringify(['%s|legacy-1']))" % company)
            lp.reload(wait_until="networkidle")
            lp.wait_for_timeout(800)
            href = lp.get_attribute("#carrylink", "href") or ""
            site = json.loads(open(os.path.join(HERE, "..", "docs", "data", "jobs.json")).read())["site_url"]
            check("no endpoint: sync is off and says nothing about it",
                  "sync" not in lp.inner_text("#freshness"), lp.inner_text("#freshness"))
            check("no endpoint: a pre-sync tick list is offered for carrying",
                  lp.locator("#carry").is_visible() and href.startswith(site + "#/import/"), href[:60])
            carried = json.loads(base64.urlsafe_b64decode(href.split("#/import/")[1] + "==="))
            check("the carried payload holds the old ticks", company + "|legacy-1" in carried["ticks"])
            local.shutdown()
            b.close()
    finally:
        server.terminate()

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
