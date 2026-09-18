"""Static export + publish. The deployed artifact is inert files; these tests
pin what may appear in them and that publishing stays a single orphan commit."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fakes import Clock
from watcher import export, publish
from watcher.store import Store

CFG = {
    "defaults": {"title": ["software engineer", "backend"], "location": ["india", "bengaluru"]},
    "companies": {"acme": {"ats": "greenhouse", "token": "acme"},
                  "globex": {"ats": "lever", "token": "globex"},
                  "dead": {"ats": "unsupported", "note": "nope"}},
}


class Row:
    def __init__(self, company, job_id, title, location, first_seen, last_seen, req="R1", ats="greenhouse"):
        self.d = {"company": company, "job_id": job_id, "title": title, "location": location,
                  "url": f"https://jobs.example.com/{company}/{job_id}", "req_id": req,
                  "first_seen": first_seen, "last_seen": last_seen, "ats": ats,
                  # fields that exist in the store and must never be exported
                  "notified": 0, "last_complete": last_seen, "posted_at": "2026-09-15"}

    def __getitem__(self, k):
        return self.d[k]


class FakeStore:
    path = "/tmp/jobs.db"

    def __init__(self, rows, polled=None):
        self._rows = rows
        self._polled = polled or {}

    def rows(self, **kw):
        return self._rows

    def state(self, company):
        return self._polled.get(company, {"last_complete": None, "last_polled": None})


class Collect(unittest.TestCase):
    def rows(self):
        r = [Row("acme", "1", "Software Engineer", "Bengaluru, India", 300, 900),
             Row("acme", "2", "Account Executive", "Bengaluru, India", 400, 900),      # title miss
             Row("globex", "3", "Backend Engineer", "London", 500, 900),               # location miss
             Row("globex", "4", "Backend Engineer", "India", 600, 900, ats="lever")]
        closed = Row("acme", "5", "Software Engineer", "India", 100, 500)
        closed.d["last_complete"] = 900                                                # closed req
        return r + [closed]

    def test_only_open_matching_rows(self):
        jobs = export.collect(CFG, FakeStore(self.rows()), now=1000)
        self.assertEqual([j["company"] + j["title"] for j in jobs],
                         ["globexBackend Engineer", "acmeSoftware Engineer"])   # newest first_seen first

    def test_freshness_is_the_data_age_not_the_render_time(self):
        """Exporting does not poll: a page rendered now over an 11h-old store says 11h."""
        polled = {"acme": {"last_complete": 600, "last_polled": 600},
                  "globex": {"last_complete": 500, "last_polled": 900}}
        store = FakeStore(self.rows(), polled)
        _, blob = export.write(CFG, store, tempfile.mkdtemp(), now=40000)
        body = json.loads(blob)
        self.assertEqual(body["generated_at"], 40000)      # when the files were written
        self.assertEqual(body["polled_at"], 600)           # when a board was last actually read
        self.assertLess(body["polled_at"], body["generated_at"])

    def test_payload_carries_only_display_fields(self):
        jobs = export.collect(CFG, FakeStore(self.rows()), now=1000)
        for j in jobs:
            self.assertEqual(sorted(j), sorted(export.FIELDS))
            # the page ticks jobs off by this key, and it is the only id every ATS gives
            self.assertTrue(j["job_id"])
        body = export.index_payload(jobs, 3, now=1000)
        self.assertEqual(body["generated_at"], 1000)
        self.assertEqual(body["watched"], 3)
        self.assertEqual(body["count"], len(jobs))

    def test_nothing_sensitive_reaches_disk(self):
        with tempfile.TemporaryDirectory() as d:
            files, blob = export.write(CFG, FakeStore(self.rows()), d, now=1000)
            text = blob.decode().lower()
            for banned in ("token", "telegram", "backoff", "blocked_until", "last_error",
                           "notified", "cursor", "tenant", "posted_at"):
                self.assertNotIn(banned, text, banned)
            # Exactly these, by path: the page, the job shards, and the one function
            # (tick sync, behind Access). Anything new here has to be added on purpose.
            self.assertEqual(sorted(os.path.relpath(f, d) for f in files),
                             ["_routes.json", "app.css", "app.js", "data/acme.json", "data/globex.json",
                              "data/jobs.json", "functions/api/state.js", "index.html"])
            page = open(os.path.join(d, "index.html")).read()
            self.assertIn('name="robots" content="noindex, nofollow"', page)
            self.assertNotIn("http://", page.replace('http://www.w3.org', ''))   # no third-party
            self.assertNotIn("https://", page.replace("https://www.w3.org", ""))

    def test_index_lists_companies_most_recently_active_first(self):
        """The home screen reads only this file — no job rows in it at all."""
        with tempfile.TemporaryDirectory() as d:
            _, blob = export.write(CFG, FakeStore(self.rows()), d, now=1000)
            index = json.loads(blob)
            self.assertNotIn("jobs", index)
            self.assertEqual([c["company"] for c in index["companies"]], ["globex", "acme"])
            self.assertEqual(index["count"], 2)
            self.assertEqual([c["count"] for c in index["companies"]], [1, 1])
            self.assertEqual(index["companies"][0]["newest"], 600)      # globex job first_seen
            self.assertEqual(index["companies"][0]["file"], "globex.json")

    def test_each_company_file_is_newest_poll_first(self):
        rows = self.rows() + [Row("acme", "9", "Backend Engineer", "India", 800, 900),
                              Row("acme", "8", "Software Engineer", "India", 200, 900)]
        with tempfile.TemporaryDirectory() as d:
            export.write(CFG, FakeStore(rows), d, now=1000)
            acme = json.load(open(os.path.join(d, "data", "acme.json")))
            self.assertEqual(acme["company"], "acme")
            self.assertEqual(acme["count"], len(acme["jobs"]))
            stamps = [j["first_seen"] for j in acme["jobs"]]
            self.assertEqual(stamps, sorted(stamps, reverse=True))       # descending poll time
            self.assertEqual(stamps, [800, 300, 200])

    def test_a_company_that_stops_matching_leaves_no_orphan_file(self):
        with tempfile.TemporaryDirectory() as d:
            export.write(CFG, FakeStore(self.rows()), d, now=1000)
            self.assertIn("globex.json", os.listdir(os.path.join(d, "data")))
            only_acme = [r for r in self.rows() if r["company"] == "acme"]
            export.write(CFG, FakeStore(only_acme), d, now=1000)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "data"))), ["acme.json", "jobs.json"])


class Publish(unittest.TestCase):
    def repo(self, d):
        subprocess.run(["git", "init", "-q", d], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", d, "config", k, v], check=True)
        open(os.path.join(d, "README.md"), "w").write("work tree\n")
        subprocess.run(["git", "-C", d, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
        return d

    def test_single_orphan_commit_that_never_touches_the_work_tree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.repo(d)
            out = os.path.join(repo, "docs")
            os.makedirs(os.path.join(out, "data"))
            open(os.path.join(out, "index.html"), "w").write("<html>1</html>")
            open(os.path.join(out, "data", "jobs.json"), "w").write("{}")
            files = [os.path.join(out, "index.html"), os.path.join(out, "data", "jobs.json")]

            publish.publish(repo, out, files, push=False)
            first = publish.git(["rev-parse", "site"], repo)
            listing = publish.git(["ls-tree", "-r", "--name-only", "site"], repo)
            self.assertEqual(sorted(listing.split()), ["data/jobs.json", "index.html"])
            self.assertEqual(publish.git(["rev-list", "--count", "site"], repo), "1")

            open(os.path.join(out, "index.html"), "w").write("<html>2</html>")
            publish.publish(repo, out, files, push=False)
            self.assertNotEqual(publish.git(["rev-parse", "site"], repo), first)
            self.assertEqual(publish.git(["rev-list", "--count", "site"], repo), "1")   # still one commit
            # HEAD still points at the working branch and no tracked file moved:
            # publishing uses a temporary index, never a checkout.
            self.assertNotEqual(publish.git(["rev-parse", "--abbrev-ref", "HEAD"], repo), "site")
            self.assertEqual(publish.git(["status", "--porcelain", "--untracked-files=no"], repo), "")

    def test_refuses_the_database_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.repo(d)
            out = os.path.join(repo, "docs")
            os.makedirs(out)
            db = os.path.join(out, "jobs.db")
            open(db, "w").write("x")
            with self.assertRaises(publish.PublishError):
                publish.publish(repo, out, [db], push=False)
            outside = os.path.join(repo, "config.yaml")
            open(outside, "w").write("x")
            with self.assertRaises(publish.PublishError):
                publish.publish(repo, out, [outside], push=False)

    def test_identical_payload_does_not_push_again(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.repo(d)
            out = os.path.join(repo, "docs")
            os.makedirs(out)
            page = os.path.join(out, "index.html")
            open(page, "w").write("<html></html>")
            state = os.path.join(d, "export.sha256")
            blob = b'{"jobs":[]}'
            first = publish.publish_quietly(repo, out, [page], blob, state, push=False)
            self.assertTrue(first.startswith("pushed"))
            again = publish.publish_quietly(repo, out, [page], blob, state, push=False)
            self.assertEqual(again, "unchanged")
            changed = publish.publish_quietly(repo, out, [page], b'{"jobs":[1]}', state, push=False)
            self.assertTrue(changed.startswith("pushed"))
            # Same jobs, new page code: that has to go out too, not wait for a job change.
            open(page, "w").write("<html>v2</html>")
            code = publish.publish_quietly(repo, out, [page], b'{"jobs":[1]}', state, push=False)
            self.assertTrue(code.startswith("pushed"), code)

    def test_push_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.repo(d)                       # no remote configured
            out = os.path.join(repo, "docs")
            os.makedirs(out)
            page = os.path.join(out, "index.html")
            open(page, "w").write("<html></html>")
            status = publish.publish_quietly(repo, out, [page], b"{}", os.path.join(d, "h"),
                                             remote="nowhere")
            self.assertTrue(status.startswith("failed:"), status)

    def test_not_a_repo_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            status = publish.publish_quietly(d, d, [], b"{}", os.path.join(d, "h"))
            self.assertIn("not a git repository", status)


class RunnerIntegration(unittest.TestCase):
    def test_export_runs_after_a_successful_run_and_is_off_by_default(self):
        from watcher import runner
        from watcher.config import export_settings
        self.assertFalse(export_settings({})["enabled"])

        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "jobs.db"))
            clock = Clock()
            cfg = dict(CFG, export={"enabled": True, "out": os.path.join(d, "site")}, companies={})
            res = runner.run(cfg, store, sink=None, http=object(), clock=clock,
                             monotonic=clock.monotonic, sleep=clock.sleep, online=lambda: True)
            store.close()
            self.assertIn("exported", res.exported)
            self.assertTrue(os.path.exists(os.path.join(d, "site", "data", "jobs.json")))

    def test_offline_run_exports_nothing(self):
        from watcher import runner
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "jobs.db"))
            clock = Clock()
            cfg = dict(CFG, export={"enabled": True, "out": os.path.join(d, "site")})
            res = runner.run(cfg, store, sink=None, http=object(), clock=clock,
                             monotonic=clock.monotonic, sleep=clock.sleep, online=lambda: False)
            store.close()
            self.assertTrue(res.offline)
            self.assertIsNone(res.exported)
            self.assertFalse(os.path.exists(os.path.join(d, "site")))


if __name__ == "__main__":
    unittest.main()


class DirectUpload(unittest.TestCase):
    """deploy: direct -- wrangler uploads the files; no Cloudflare build to queue or hang."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.wr = os.path.join(self.d.name, "wrangler")
        open(self.wr, "w").write("#!/bin/sh\n")
        os.chmod(self.wr, 0o755)
        self.env = mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.d.cleanup()

    def test_it_runs_pages_deploy_from_the_export_directory(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, cwd=kw["cwd"], path=kw["env"]["PATH"])
            return mock.Mock(returncode=0, stdout="✨ Deployment complete! https://abc123.job-watch-live.pages.dev\n",
                             stderr="")
        url = publish.publish_direct(self.d.name, "job-watch-live", self.wr, runner=run)
        self.assertEqual(url, "https://abc123.job-watch-live.pages.dev")
        self.assertEqual(seen["argv"][1:5], ["pages", "deploy", ".", "--project-name"])
        self.assertIn("job-watch-live", seen["argv"])
        self.assertEqual(seen["cwd"], self.d.name)
        self.assertTrue(seen["path"].startswith("/opt/homebrew/bin"))    # launchd has no PATH for node

    def test_no_token_is_an_error_not_a_hang(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}):
            with self.assertRaisesRegex(publish.PublishError, "CLOUDFLARE_API_TOKEN"):
                publish.publish_direct(self.d.name, "p", self.wr, runner=None)

    def test_a_failed_upload_is_reported_and_retried_next_run(self):
        fail = lambda argv, **kw: mock.Mock(returncode=1, stdout="", stderr="Authentication error [code: 10000]")
        state = os.path.join(self.d.name, "h")
        with mock.patch.object(publish.subprocess, "run", fail):
            status = publish.publish_quietly(self.d.name, self.d.name, [], b"{}", state,
                                             deploy="direct", project="p", wrangler=self.wr)
        self.assertIn("Authentication error", status)
        self.assertFalse(os.path.exists(state))                            # not marked as published

    def test_export_writes_the_wrangler_config_with_the_kv_binding(self):
        cfg = dict(CFG, export={"deploy": "direct", "project": "job-watch-live", "kv_state": "abc"})
        files, _ = export.write(cfg, FakeStore([]), self.d.name, now=1000)
        toml = open(os.path.join(self.d.name, "wrangler.toml")).read()
        self.assertIn('name = "job-watch-live"', toml)
        self.assertIn('binding = "STATE"', toml)
        self.assertIn('id = "abc"', toml)
