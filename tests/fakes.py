import json
import os

from watcher.http import HttpStatusError, NotModified

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class FakeHttp:
    """Stands in for HttpClient. `routes` maps a URL (or a callable) to a response.

    A response may be a dict/list (JSON), an Exception to raise, or a callable
    taking (method, url, payload, cache) and returning one of those.
    """

    def __init__(self, routes=None, etag=None):
        self.routes = routes or {}
        self.calls = []
        self.etag = etag
        self.requests = 0

    def _respond(self, method, url, payload=None, cache=None):
        self.calls.append((method, url, payload))
        self.requests += 1
        handler = self.routes.get(url)
        if handler is None:
            for key, h in self.routes.items():
                if callable(key) and key(method, url, payload):
                    handler = h
                    break
        if handler is None:
            raise HttpStatusError(url, 404)
        resp = handler(method, url, payload, cache) if callable(handler) else handler
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get_json(self, url, cache=None, headers=None):
        if cache and self.etag and cache.get("etag") == self.etag:
            self.calls.append(("GET", url, None))
            self.requests += 1
            raise NotModified(url)
        resp = self._respond("GET", url, cache=cache)
        if cache is not None and self.etag:
            cache.clear()
            cache["etag"] = self.etag
        return json.loads(json.dumps(resp))

    def post_json(self, url, payload, headers=None):
        return json.loads(json.dumps(self._respond("POST", url, payload)))

    def get_text(self, url, headers=None):
        return self._respond("GET", url)


def page(name):
    """A saved web page, for the HTML adapters."""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class RecordingSink:
    name = "recording"
    max_len = 4096

    def __init__(self, ok=True):
        self.messages = []
        self.ok = ok

    def esc(self, s):
        return s

    def bold(self, s):
        return s

    def code(self, s):
        return s

    def link(self, url, text):
        return url

    def send(self, text):
        self.messages.append(text)
        return self.ok


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = float(t)
        self.mono = 0.0

    def __call__(self):
        return self.t

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.t += seconds

    def sleep(self, seconds):
        self.mono += seconds
