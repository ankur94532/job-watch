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
import json
import os
import re
import socketserver
import sys
import threading
import urllib.parse

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

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/jobs":
                query = urllib.parse.parse_qs(url.query)
                name = (query.get("file") or ["jobs.json"])[0]
                if name != os.path.basename(name) or not name.endswith(".json"):
                    self.send_error(404)
                    return
                path = os.path.join(ROOT, "data", name)
                if not os.path.isfile(path):
                    self.send_error(404)
                    return
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    socketserver.TCPServer.allow_reuse_address = True    # back-to-back runs: TIME_WAIT
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
    with open(os.path.join(ROOT, "data", "jobs.json")) as f:
        index = json.load(f)

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

        # Anything marked hidden must actually be invisible: .refresh set
        # `display: block`, which beat the attribute, and an empty pill sat
        # above the list on every visit.
        leaks = p.evaluate("""() => [...document.querySelectorAll('[hidden]')]
            .filter(e => getComputedStyle(e).display !== 'none').map(e => e.id || e.className)""")
        check("nothing marked hidden is visible", leaks == [], repr(leaks))

        rows = p.locator(".company-row")
        n = rows.count()
        check("all watched companies are visible", n == index["watched"],
              "%d rows / %d watched" % (n, index["watched"]))
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

        # ---- a realistic list: only a few companies have something new ------
        # Mark everything read at one poll, then make three companies newer than
        # it. With every company dotted (above), "leaves the top" cannot show.
        p.evaluate("""() => { localStorage.clear();
            localStorage.setItem('jobwatch:seenThrough', '1'); }""")
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        names = p.eval_on_selector_all(".company-row .company-name", "els => els.map(e => e.textContent)")
        fresh3 = names[:3]
        p.evaluate("""(fresh) => {
            const v = {};
            [...document.querySelectorAll('.company-row .company-name')].forEach(e => v[e.textContent] = 9e9);
            fresh.forEach(nm => v[nm] = 1);           // seen long ago: these three are new
            localStorage.setItem('jobwatch:visited', JSON.stringify(v)); }""", fresh3)
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        order = p.eval_on_selector_all(".company-row .company-name", "els => els.map(e => e.textContent)")
        flags = p.eval_on_selector_all(".company-row", "els => els.map(e => !e.querySelector('.dot').hidden)")
        check("exactly the new companies carry a dot", sum(flags) == 3, "%d dotted" % sum(flags))
        check("the dotted companies are the top rows", flags[:3] == [True] * 3 and not any(flags[3:]))
        rest = order[3:]
        check("everything below the dotted group is A-Z",
              rest == sorted(rest, key=str.lower), "first few: %s" % rest[:4])

        top = order[0]
        p.evaluate("(nm) => [...document.querySelectorAll('.company-row')]"
                   ".find(e => e.querySelector('.company-name').textContent === nm).click()", top)
        p.wait_for_timeout(1000)
        p.go_back()
        p.wait_for_timeout(1000)
        order = p.eval_on_selector_all(".company-row .company-name", "els => els.map(e => e.textContent)")
        flags = p.eval_on_selector_all(".company-row", "els => els.map(e => !e.querySelector('.dot').hidden)")
        # Not merely "below the dotted group": the old order put it directly
        # beneath it, which on screen is still the top.
        want = 2 + sorted(order[2:], key=str.lower).index(top)
        check("an opened company leaves the top for its A-Z place",
              sum(flags) == 2 and order.index(top) == want,
              "%r at row %d, A-Z row %d" % (top, order.index(top), want))
        check("it goes back to its A-Z place", order[2:] == sorted(order[2:], key=str.lower))

        # ---- the place in the list survives the tab being reloaded ---------
        p.evaluate("window.scrollTo(0, 900)")
        p.wait_for_timeout(600)
        before = p.evaluate("window.pageYOffset")
        idx = p.evaluate("""() => [...document.querySelectorAll('.company-row')].findIndex(e => {
            const r = e.getBoundingClientRect();
            return r.top > 80 && r.bottom < window.innerHeight - 80; })""")
        p.evaluate("(i) => document.querySelectorAll('.company-row')[i].click()", idx)
        p.wait_for_timeout(1000)
        p.reload(wait_until="networkidle")        # as Memory Saver does to a background tab
        p.wait_for_selector(".job, #empty:not([hidden])")
        p.click("#back")
        p.wait_for_timeout(1200)
        after = p.evaluate("window.pageYOffset")
        check("back after the tab reloaded still lands where you were", abs(after - before) <= 60,
              "was %s, now %s" % (before, after))

        # ---- unticked counts ------------------------------------------------
        p.evaluate("localStorage.clear()")
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        first = next(c["company"] for c in index["companies"] if c["count"] > 1)
        p.evaluate("""(nm) => [...document.querySelectorAll('.company-row')]
            .find(e => e.querySelector('.company-name').textContent === nm).click()""", first)
        p.wait_for_timeout(1200)
        p.wait_for_selector(".job .tick")
        # Count the whole filtered list, not the rendered page: on a board with
        # more than a page of rows the next one slides in and the page stays full.
        of = lambda: int(re.search(r"of (\d+)", p.inner_text("#summary")).group(1))
        total = of()
        first_title = p.locator(".job .title").first.inner_text()
        p.locator(".job .tick").first.click()
        p.wait_for_timeout(500)
        check("ticking hides the job", of() == total - 1 and
              p.locator(".job .title").first.inner_text() != first_title,
              "%d -> %d" % (total, of()))
        p.go_back()
        p.wait_for_timeout(1200)
        meta = row_meta(p, first)
        m = re.match(r"(\d+) unticked of (\d+)", meta or "")
        check("home shows unticked of open, one fewer than the total",
              bool(m) and int(m.group(1)) == int(m.group(2)) - 1, repr(meta))
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        check("the unticked count survives a reload", row_meta(p, first) == meta)

        # A tick on a job that has since CLOSED must not count: the home screen
        # used to subtract every stored tick and read "0 unticked of 29".
        p.evaluate("""(nm) => { const t = JSON.parse(localStorage.getItem('jobwatch:tickLog') || '{}');
            t[nm + '|long-closed-1'] = [1, Date.now()]; t[nm + '|long-closed-2'] = [1, Date.now()];
            localStorage.setItem('jobwatch:tickLog', JSON.stringify(t)); }""", first)
        p.reload(wait_until="networkidle")
        p.wait_for_selector(".company-row")
        check("ticks on closed jobs do not reduce the unticked count", row_meta(p, first) == meta,
              "%r vs %r" % (row_meta(p, first), meta))

        empty_names = [c["company"] for c in index["companies"] if c["count"] == 0]
        if empty_names:
            check("empty companies have an explicit label",
                  all(row_meta(p, nm) == "No matching open roles" for nm in empty_names),
                  "%d empty companies" % len(empty_names))
            check("empty companies have no new-job dot",
                  all(dot_hidden(p, nm) for nm in empty_names))
            p.evaluate("""(nm) => [...document.querySelectorAll('.company-row')]
                .find(e => e.querySelector('.company-name').textContent === nm).click()""",
                       empty_names[0])
            p.wait_for_selector("#empty:not([hidden])")
            check("empty company opens without a fetch error",
                  p.locator(".job").count() == 0 and "of 0" in p.inner_text("#summary"))

        b.close()

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
