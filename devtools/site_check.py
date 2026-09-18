"""Drive the exported phone view in a real browser. DEV-ONLY, like `watcher trace`.

The site is plain DOM with no build step, so the only honest test is a real
browser against the real `docs/` export. Playwright is not a runtime dependency
and nothing here is imported by the poller:

    .venv-dev/bin/python devtools/site_check.py

It serves `docs/` on a spare port, drives it, and prints one line per check.

One harness trap worth knowing: **do not use Playwright's `locator.click()`**
for the company rows. It scrolls the element into view before dispatching, which
moves the page and silently invalidates any assertion about scroll position. A
thumb does not do that. These checks dispatch a DOM `click()` instead.
"""

import http.server
import os
import re
import socketserver
import sys
import threading

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "docs")
PORT = 8791
fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + detail if detail else ""))
    if not cond:
        fails.append(name)


def serve():
    os.chdir(ROOT)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def row_meta(p, name):
    return p.evaluate("""(nm) => { const r = [...document.querySelectorAll('.company-row')]
        .find(e => e.querySelector('.company-name').textContent === nm);
        return r ? r.querySelector('.company-meta').textContent : null; }""", name)


def dot_hidden(p, name):
    return p.evaluate("""(nm) => { const r = [...document.querySelectorAll('.company-row')]
        .find(e => e.querySelector('.company-name').textContent === nm);
        return r ? r.querySelector('.dot').hidden : null; }""", name)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed, and deliberately is not a runtime dependency.\n"
            "  python3 -m venv .venv-dev && .venv-dev/bin/pip install playwright\n"
            "  .venv-dev/bin/playwright install chromium")

    serve()
    base = "http://127.0.0.1:%d/index.html" % PORT

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        # phone-sized, so the company list actually overflows and can scroll
        c = b.new_context(viewport={"width": 390, "height": 720})
        p = c.new_page()
        p.goto(base, wait_until="networkidle")
        p.wait_for_selector(".company-row")
        # an ancient baseline, so every company counts as having something new
        p.evaluate("localStorage.clear(); localStorage.setItem('jobwatch:seenThrough','1')")
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")

        rows = p.locator(".company-row")
        n = rows.count()
        dots = p.locator(".company-row .dot:not([hidden])").count()
        check("green dot on companies with new jobs", dots > 0, "%d/%d dotted" % (dots, n))

        flags = p.eval_on_selector_all(
            ".company-row", "els => els.map(e => !e.querySelector('.dot').hidden)")
        cut = flags.index(False) if False in flags else len(flags)
        check("dotted companies sort above the rest", not any(flags[cut:]),
              "%d dotted, first undotted at %d" % (sum(flags), cut))

        # ---- scroll restore ------------------------------------------------
        p.evaluate("window.scrollTo(0, 1000)")
        p.wait_for_timeout(600)
        before = p.evaluate("window.pageYOffset")
        check("home is scrolled before navigating", before > 100, "y=%s" % before)

        idx = p.evaluate("""() => [...document.querySelectorAll('.company-row')].findIndex(e => {
            const r = e.getBoundingClientRect();
            return r.top > 80 && r.bottom < window.innerHeight - 80; })""")
        check("found an on-screen row to tap", idx >= 0, "index %s" % idx)
        name = rows.nth(idx).locator(".company-name").inner_text()
        at_tap = p.evaluate("window.pageYOffset")
        p.evaluate("(i) => document.querySelectorAll('.company-row')[i].click()", idx)
        p.wait_for_timeout(1200)
        check("the tap did not itself move the page", at_tap == before,
              "before=%s at tap=%s" % (before, at_tap))
        check("company screen opens at the top", p.evaluate("window.pageYOffset") < 40)

        p.go_back()
        p.wait_for_timeout(1500)
        after = p.evaluate("window.pageYOffset")
        check("back to home restores the scroll position", abs(after - before) <= 60,
              "was %s, now %s" % (before, after))

        # ---- the dot is per company and clears on open ---------------------
        check("opening one company clears exactly one dot",
              p.locator(".company-row .dot:not([hidden])").count() == dots - 1)
        check("the opened company has no dot, though nothing was ticked",
              dot_hidden(p, name) is True, "company=%r" % name)
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        check("the cleared dot stays cleared after a reload", dot_hidden(p, name) is True)
        p.evaluate("""(nm) => { const v = JSON.parse(localStorage.getItem('jobwatch:visited')||'{}');
            v[nm] = (v[nm]||0) - 3600;      // as if the watcher just found something newer
            localStorage.setItem('jobwatch:visited', JSON.stringify(v)); }""", name)
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        check("a newer poll brings the dot back", dot_hidden(p, name) is False)

        # ---- unticked counts ------------------------------------------------
        p.evaluate("localStorage.clear()")
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        first = p.evaluate("""() => document.querySelectorAll('.company-row')[0]
            .querySelector('.company-name').textContent""")
        p.evaluate("""(nm) => [...document.querySelectorAll('.company-row')]
            .find(e => e.querySelector('.company-name').textContent === nm).click()""", first)
        p.wait_for_timeout(1200)
        p.wait_for_selector(".job .tick")
        total = p.locator(".job").count()
        p.locator(".job .tick").first.click()
        p.wait_for_timeout(500)
        check("ticking hides the job", p.locator(".job").count() == total - 1)
        p.go_back()
        p.wait_for_timeout(1200)
        meta = row_meta(p, first)
        m = re.match(r"(\d+) unticked of (\d+)", meta or "")
        check("home shows unticked of open, one fewer than the total",
              bool(m) and int(m.group(1)) == int(m.group(2)) - 1, repr(meta))
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        check("the unticked count survives a reload", row_meta(p, first) == meta)

        b.close()

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
