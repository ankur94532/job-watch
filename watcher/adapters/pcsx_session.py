"""Ordinary anonymous PCSX session, following the public frontend protocol.

No login, browser identity, challenge handling or imported browser cookies.
Persist only this client's server-issued cookies so process restarts do not
rotate its identity or defeat cookie-scoped limits. CSRF context comes fresh
from the public careers shell and is never written to a paging cursor.
"""

import http.cookiejar
import os
import re
import tempfile
import time
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..http import HttpClient, _json, network_up, user_agent
from ..robots import fetch as fetch_robots
from ..store import db_path
from .base import AdapterError


class _Token(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "_csrf":
            self.token = values.get("content")


class PCSXSession:
    def __init__(self, host, parent, directory=None):
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise AdapterError("invalid PCSX frontend host")
        self.host = host
        self.page = "https://" + host + "/careers?location=India&hl=en"
        directory = directory or os.path.join(os.path.dirname(db_path()), "frontend-sessions")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, host + ".cookies")
        self.jar = http.cookiejar.MozillaCookieJar(self.path)
        if os.path.exists(self.path):
            # A corrupt jar fails rather than silently rotating an established
            # session whose quota may still be in force.
            try:
                self.jar.load(ignore_discard=True)
            except OSError:
                raise AdapterError("Stored PCSX session cannot be loaded; refusing identity rotation") from None
        self.http = self._make_http(parent)
        self.token, self.rules = None, None

    def _make_http(self, parent):
        host = self.host

        class SameHost(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                parsed = urlsplit(newurl)
                if parsed.scheme != "https" or parsed.netloc != host:
                    raise AdapterError("PCSX frontend redirected outside its public host")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(
            SameHost(), urllib.request.HTTPCookieProcessor(self.jar))
        return HttpClient(timeout=getattr(parent, "timeout", 15),
                          retries=getattr(parent, "retries", 2),
                          online=getattr(parent, "online", network_up), opener=opener.open)

    def _save(self):
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".cookies-")
        os.close(fd)
        try:
            self.jar.save(temporary, ignore_discard=True)
            os.replace(temporary, self.path)  # mkstemp keeps credentials mode 600
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_json(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != self.host or not parsed.path.startswith("/api/pcsx/"):
            raise AdapterError("PCSX session refuses an off-host/non-PCSX request")
        if self.rules is None:
            self.rules = fetch_robots(self.page)
        if not all(self.rules.allowed(user_agent(), u) for u in (self.page, url)):
            raise AdapterError("robots.txt forbids PCSX frontend/session request")
        try:
            if not self.token:
                parser = _Token()
                parser.feed(self.http.get_text(self.page))
                if not parser.token:
                    raise AdapterError("PCSX public shell has no CSRF context; refusing API request")
                self.token = parser.token
            body, headers = self.http.request("GET", url, headers={
                "Referer": self.page, "X-CSRF-Token": self.token,
                "X-Browser-Request-Time": str(time.time()),
            })
            self.token = headers.get("X-CSRF-Token") or self.token
            return _json(body, url)
        finally:
            self._save()
