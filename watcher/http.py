"""Stdlib HTTP client with polite defaults.

* Honest User-Agent that names the tool and links the repo. Never a browser UA.
* Retries only 5xx and timeouts, briefly (a run must stay under ~60s); honours
  Retry-After. 4xx is never retried; 429/403 surface to the runner, which
  persists a per-company backoff.
* Conditional GET: pass a `cache` dict; ETag / Last-Modified are sent back and
  a 304 raises NotModified.
"""

import email.utils
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request

from . import __version__

log = logging.getLogger(__name__)


def user_agent():
    contact = os.environ.get("JOBSWATCH_CONTACT_URL", "").strip()
    return f"job-watch/{__version__} (" + (f"+{contact}; " if contact else "") + "personal job-alert poller)"


def jitter(seconds, spread=0.3):
    return seconds * random.uniform(1 - spread, 1 + spread) if seconds > 0 else 0


class NotModified(Exception):
    pass


class HttpStatusError(Exception):
    def __init__(self, url, code, retry_after=None, body=b""):
        super().__init__(f"HTTP {code} from {url}")
        self.url = url
        self.code = code
        self.retry_after = retry_after
        self.body = body


class NetworkError(Exception):
    """No HTTP response at all: DNS failure, refused, timeout, TLS."""


class InvalidResponse(Exception):
    """200 OK, but not the JSON we asked for.

    Usually a redirect into an HTML page: Workday tenants bounce to
    community.workday.com/maintenance-page during maintenance, and a careers
    host under a bot check serves a challenge page. Both are transient and the
    next run should just try again, so this is an ordinary company failure
    rather than a crash.
    """


def parse_retry_after(value, now=None):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0, int(when - (now or time.time())))


def network_up(host="captive.apple.com", port=80, timeout=3.0):
    """Same reachability probe macOS itself uses after waking."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


class HttpClient:
    def __init__(self, timeout=15, retries=2, backoff=1.0, max_retry_wait=10, sleep=time.sleep, opener=None,
                 online=network_up):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_retry_wait = max_retry_wait
        self.sleep = sleep
        self.opener = opener or urllib.request.urlopen
        self.online = online
        self.requests = 0

    def get_json(self, url, cache=None, headers=None):
        h = dict(headers or {})
        if cache:
            if cache.get("etag"):
                h["If-None-Match"] = cache["etag"]
            if cache.get("last_modified"):
                h["If-Modified-Since"] = cache["last_modified"]
        body, resp_headers = self.request("GET", url, headers=h)
        parsed = _json(body, url)
        if cache is not None:
            cache.clear()
            if resp_headers.get("ETag"):
                cache["etag"] = resp_headers["ETag"]
            if resp_headers.get("Last-Modified"):
                cache["last_modified"] = resp_headers["Last-Modified"]
        return parsed

    def post_json(self, url, payload, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        body, _ = self.request("POST", url, data=json.dumps(payload).encode(), headers=h)
        return _json(body, url)

    def request(self, method, url, data=None, headers=None):
        h = {"Accept": "application/json", "User-Agent": user_agent()}
        h.update(headers or {})
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            self.requests += 1
            try:
                with self.opener(req, timeout=self.timeout) as r:
                    return r.read(), r.headers
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    raise NotModified(url) from None
                retry_after = parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
                err = HttpStatusError(url, e.code, retry_after, _safe_read(e))
                if e.code < 500 or attempt >= self.retries:
                    raise err from None
                wait = retry_after if retry_after is not None else self.backoff * (2**attempt)
                if wait > self.max_retry_wait:
                    raise err from None
                reason = f"HTTP {e.code}"
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                reason = str(getattr(e, "reason", e))
                if attempt >= self.retries or not self.online():
                    raise NetworkError(f"{url}: {reason}") from None
                wait = self.backoff * (2**attempt)
            wait = jitter(wait)
            log.warning("%s %s failed (%s), retry %d in %.1fs", method, url, reason, attempt + 1, wait)
            self.sleep(wait)
            attempt += 1


def _json(body, url):
    try:
        return json.loads(body)
    except ValueError:
        snippet = body[:80].decode("utf-8", "replace").replace("\n", " ").strip()
        raise InvalidResponse(f"{url}: expected JSON, got {len(body)} bytes of {snippet!r}") from None


def _safe_read(e):
    try:
        return e.read()
    except Exception:
        return b""
