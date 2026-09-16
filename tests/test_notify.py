import io
import json
import unittest
import urllib.error

from watcher import notify
from watcher.filters import Filter

NOW = 1_800_000_000


def alert(i, company="acme", title="Software Engineer", location="Bengaluru, India", posted="2026-09-15", **kw):
    a = {"company": company, "job_id": str(i), "title": title, "location": location, "posted_at": posted,
         "url": f"https://jobs.example.com/{company}/{i}", "req_id": f"R-{i}", "first_seen": NOW - 60}
    a.update(kw)
    return a


class Compose(unittest.TestCase):
    def setUp(self):
        self.sink = notify.TelegramSink("t", "c")

    def test_nothing_new_sends_nothing(self):
        self.assertEqual(notify.compose(self.sink, [], now=NOW), [])

    def test_grouped_by_company_with_headers_one_message(self):
        alerts = [alert(1, "acme"), alert(2, "globex"), alert(3, "acme")]
        msgs = notify.compose(self.sink, alerts, now=NOW)
        self.assertEqual(len(msgs), 1)
        text, keys = msgs[0]
        self.assertIn("<b>▍acme (2 new)</b>", text)
        self.assertIn("<b>▍globex (1 new)</b>", text)
        self.assertEqual(text.count("▍acme"), 1)
        self.assertEqual(sorted(keys), [("acme", "1"), ("acme", "3"), ("globex", "2")])
        self.assertIn('<a href="https://jobs.example.com/acme/1">Apply</a>', text)
        self.assertIn("<code>R-1</code>", text)

    def test_iso_timestamps_render_as_dates(self):
        text, _ = notify.compose(self.sink, [alert(1, posted="2026-09-06T08:47:22-04:00")], now=NOW)[0]
        self.assertIn("🗓 2026-09-06", text)
        self.assertNotIn("08:47:22", text)
        text, _ = notify.compose(self.sink, [alert(2, posted="Posted Today")], now=NOW)[0]
        self.assertIn("🗓 Posted Today", text)

    def test_html_is_escaped(self):
        text, _ = notify.compose(self.sink, [alert(1, title="C++ & <Rust> Engineer")], now=NOW)[0]
        self.assertIn("C++ &amp; &lt;Rust&gt; Engineer", text)

    def test_splits_under_4096_at_entry_boundaries_and_numbers_parts(self):
        long_loc = "Bengaluru, India | " + "Somewhere Else | " * 12
        alerts = [alert(i, company=f"co{i % 3}", location=long_loc) for i in range(15)]
        for a in alerts:
            a["title"] = "Senior Software Engineer, Distributed Search Infrastructure " + a["job_id"]
        msgs = notify.compose(self.sink, alerts, now=NOW, digest_threshold=100)
        self.assertGreater(len(msgs), 1)
        for n, (text, _) in enumerate(msgs, 1):
            self.assertLessEqual(len(text), 4096)
            self.assertTrue(text.startswith(f"({n}/{len(msgs)})\n"))
        body = "\n".join(t for t, _ in msgs)
        for a in alerts:
            self.assertEqual(body.count('"%s"' % a["url"]), 1)       # every entry exactly once, never cut
        keys = [k for _, ks in msgs for k in ks]
        self.assertEqual(sorted(keys), sorted((a["company"], a["job_id"]) for a in alerts))

    def test_digest_above_threshold(self):
        alerts = [alert(i, company=f"co{i % 4}") for i in range(40)]
        alerts[17]["title"] = "Backend Engineer"
        msgs = notify.compose(self.sink, alerts, now=NOW)
        self.assertEqual(len(msgs), 1)
        text, keys = msgs[0]
        self.assertIn("40 new matches", text)
        self.assertIn("across 4 companies", text)
        self.assertIn("co0: 10", text)
        self.assertEqual(text.count("Apply</a>"), 5)
        self.assertIn("…and 35 more", text)
        self.assertIn("watcher list --since", text)
        self.assertEqual(len(keys), 40)

    def test_exactly_threshold_is_not_a_digest(self):
        msgs = notify.compose(self.sink, [alert(i) for i in range(15)], now=NOW)
        self.assertNotIn("new matches", msgs[0][0])


class Ordering(unittest.TestCase):
    def test_relevance_then_newest(self):
        f = Filter(title=["software engineer", "platform"], location=["india", "remote"])
        alerts = [
            alert(1, title="Platform Engineer"),
            alert(2, title="Software Engineer", location="Remote"),
            alert(3, title="Software Engineer", posted="2026-09-01"),
            alert(4, title="Software Engineer", posted="Posted Today"),
        ]
        for a in alerts:
            a["score"] = f.score(a["title"], a["location"])
        ranked = [a["job_id"] for a in notify.order(alerts, now=NOW)]
        self.assertEqual(ranked, ["4", "3", "1", "2"])

    def test_workday_relative_dates(self):
        self.assertEqual(notify.age_days("Posted Today", None, NOW), 0)
        self.assertEqual(notify.age_days("Posted Yesterday", None, NOW), 1)
        self.assertEqual(notify.age_days("Posted 3 Days Ago", None, NOW), 3)
        self.assertEqual(notify.age_days("Posted 30+ Days Ago", None, NOW), 30)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Telegram(unittest.TestCase):
    def test_429_honours_retry_after_and_paces_sends(self):
        sleeps, calls = [], []
        t = {"now": 0.0}

        def opener(req, timeout):
            calls.append(json.loads(req.data))
            if len(calls) == 1:
                body = io.BytesIO(json.dumps({"ok": False, "parameters": {"retry_after": 7}}).encode())
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, body)
            return FakeResponse(b'{"ok": true}')

        def sleep(s):
            sleeps.append(s)
            t["now"] += s

        sink = notify.TelegramSink("tok", "chat", sleep=sleep, opener=opener, clock=lambda: t["now"])
        self.assertTrue(sink.send("one"))
        self.assertIn(7.0, sleeps)
        self.assertTrue(sink.send("two"))
        self.assertEqual(calls[0]["parse_mode"], "HTML")
        self.assertEqual(len(calls), 3)

    def test_unreachable_returns_false(self):
        def opener(req, timeout):
            raise urllib.error.URLError("no route")

        self.assertFalse(notify.TelegramSink("t", "c", opener=opener, sleep=lambda s: None).send("x"))

    def test_from_env(self):
        self.assertIsInstance(notify.from_env({}), notify.ConsoleSink)
        self.assertIsInstance(notify.from_env({"TELEGRAM_BOT_TOKEN": "a", "TELEGRAM_CHAT_ID": "b"}), notify.TelegramSink)


if __name__ == "__main__":
    unittest.main()
