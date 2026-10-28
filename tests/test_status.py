import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watcher import cli
from watcher.store import Store


NOW = 1_800_000_000


class Status(unittest.TestCase):
    def render(self, state, **company):
        cfg = {"defaults": {}, "companies": {
            "acme": {"ats": "microsoft", "interval": 900, "incremental": True,
                     "full_sweep": 86400, **company},
        }}
        store = Store(":memory:")
        self.addCleanup(store.close)
        store._set("acme", **{
            "last_polled": NOW - 60, "last_complete": NOW - 26 * 86400,
            "last_count": 87, **state,
        })
        output = io.StringIO()
        with patch.object(cli.config_mod, "load", return_value=cfg), \
                patch.object(cli, "Store", return_value=store), \
                patch.object(store, "close"), \
                patch.object(cli.time, "time", return_value=NOW), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_status(SimpleNamespace(config="unused", db=":memory:")), 0)
        return output.getvalue()

    def test_cursor_does_not_hide_overdue_closure_sweep(self):
        output = self.render({"cursor": {"start": 19}})
        self.assertIn("STARVING: no complete sweep in 624.0h, 87 open", output)
        self.assertIn("sweep in progress (at 19)", output)

    def test_backoff_retains_inventory_age_and_cursor(self):
        output = self.render({"blocked_until": NOW + 3600, "last_error": "HTTP 429",
                              "cursor": {"offset": 200}})
        self.assertIn("BACKED OFF", output)
        self.assertIn("HTTP 429", output)
        self.assertIn("STARVING: no complete sweep", output)
        self.assertIn("sweep in progress (at 200)", output)

    def test_missed_polls_do_not_hide_overdue_closure_sweep(self):
        output = self.render({"last_polled": NOW - 6 * 3600})
        self.assertIn("STARVING: not polled in 6.0h", output)
        self.assertIn("STARVING: no complete sweep", output)

    def test_recent_inventory_respects_daily_full_sweep_allowance(self):
        for cursor in (None, {"offset": 20}):
            with self.subTest(cursor=cursor):
                output = self.render({"last_complete": NOW - 12 * 3600, "cursor": cursor})
                self.assertNotIn("STARVING", output)
                self.assertIn("sweep in progress" if cursor else "ok, 87 open", output)
