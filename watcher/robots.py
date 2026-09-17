"""A robots.txt matcher that understands query strings and wildcards (RFC 9309).

`urllib.robotparser` cannot be used for this. It normalises each rule through
`urlparse(...).path`, which **drops the query string**, so Google's

    Disallow: /about/careers/applications/jobs/results?page=

becomes `Disallow: /about/careers/applications/jobs/results` and blocks the
unpaginated results page too — a rule the publisher did not write. Worse, a rule
like `Disallow: /?` collapses to `Disallow: /` and blocks the entire site. Both
happen on google.com/robots.txt today, which is how a "Google disallows page 1"
conclusion appears out of nowhere.

This implements the matching RFC 9309 actually specifies:

* groups keyed by user-agent, matched on a case-insensitive prefix of the
  product token, with `*` as the fallback group and the most specific match
  winning;
* `*` matching any run of characters and `$` anchoring the end;
* rules compared against path **plus query**;
* longest matching rule wins, and Allow beats Disallow on an equal-length tie.

Nothing here fetches on a schedule: this is a gate used before a fetch, not a
crawler.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from .http import user_agent

__all__ = ["Robots", "fetch", "allows"]


def _rule_to_regex(path):
    out, i = ["^"], 0
    while i < len(path):
        ch = path[i]
        if ch == "*":
            out.append(".*")
        elif ch == "$" and i == len(path) - 1:
            out.append("$")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out))


class Robots:
    def __init__(self, text="", status=200):
        self.groups = {}          # agent (lowercased) -> [(allow?, raw path, regex)]
        self.status = status
        self._parse(text or "")

    def _parse(self, text):
        agents, seen_rule = [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if seen_rule:          # a new group starts after the previous one's rules
                    agents, seen_rule = [], False
                agents.append(value.lower())
                self.groups.setdefault(value.lower(), [])
            elif field in ("allow", "disallow") and agents:
                seen_rule = True
                if field == "disallow" and value == "":
                    continue           # "Disallow:" with no path allows everything
                for a in agents:
                    self.groups[a].append((field == "allow", value, _rule_to_regex(value)))

    def group_for(self, agent):
        """Most specific matching group, else `*`, else nothing."""
        agent = (agent or "").lower()
        best = None
        for token, rules in self.groups.items():
            if token == "*":
                continue
            if token and token in agent:
                if best is None or len(token) > len(best[0]):
                    best = (token, rules)
        if best:
            return best[1]
        return self.groups.get("*", [])

    def allowed(self, agent, url):
        """RFC 9309: longest match wins; Allow wins an equal-length tie."""
        parts = urllib.parse.urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        decision, strength = True, -1
        for allow, path, rx in self.group_for(agent):
            if rx.match(target):
                weight = len(path)
                if weight > strength or (weight == strength and allow):
                    decision, strength = allow, weight
        return decision

    def why(self, agent, url):
        """The winning rule, for logs and for writing an honest note in config."""
        parts = urllib.parse.urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        best = None
        for allow, path, rx in self.group_for(agent):
            if rx.match(target) and (best is None or len(path) > len(best[1])):
                best = (allow, path)
        return None if best is None else ("Allow: " if best[0] else "Disallow: ") + best[1]


def fetch(url, timeout=15):
    """Read the robots.txt governing `url`. A missing file allows everything."""
    parts = urllib.parse.urlsplit(url)
    target = f"{parts.scheme}://{parts.netloc}/robots.txt"
    req = urllib.request.Request(target, headers={"User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Robots(r.read(512_000).decode("utf-8", "replace"), r.status)
    except urllib.error.HTTPError as e:
        return Robots("", e.code)          # 404/410: nothing disallowed
    except Exception:
        return Robots("", 0)               # unreachable: do not invent a prohibition


def allows(url, agent=None, timeout=15):
    r = fetch(url, timeout)
    return r.allowed(agent or user_agent(), url), r.why(agent or user_agent(), url)
