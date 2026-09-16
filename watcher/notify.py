"""Notifications: compose a run's matches into few messages, deliver to a sink.

A sink is anything with `max_len`, the small markup helpers below, and
`send(text) -> bool`. Telegram and stdout are provided; drop in another by
subclassing Sink.

Rules (see DEPLOY.md):
* never one message per job — every new match of a run is batched, grouped by
  company, split at entry boundaries under the sink's length cap, parts numbered;
* above `digest_threshold` matches, a digest instead: counts per company, the
  top few in full, and a pointer to `watcher list` for the rest;
* most relevant first; newest posted breaks ties;
* nothing new → nothing sent.
"""

import html
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DIGEST_THRESHOLD = 15
DIGEST_TOP = 5


class Sink:
    name = "base"
    max_len = 4096

    def esc(self, s):
        return s

    def bold(self, s):
        return self.esc(s)

    def code(self, s):
        return self.esc(s)

    def link(self, url, text):
        return url

    def send(self, text):
        raise NotImplementedError


class ConsoleSink(Sink):
    name = "console"

    def send(self, text):
        print(text + "\n", flush=True)
        return True


class TelegramSink(Sink):
    name = "telegram"
    min_gap = 1.1           # seconds between messages; Telegram throttles bursts

    def __init__(self, token, chat_id, timeout=20, sleep=time.sleep, opener=None, clock=time.monotonic):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.sleep = sleep
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self._last = None

    def esc(self, s):
        return html.escape(s or "", quote=False)

    def bold(self, s):
        return f"<b>{self.esc(s)}</b>"

    def code(self, s):
        return f"<code>{self.esc(s)}</code>"

    def link(self, url, text):
        return '<a href="' + html.escape(url or "", quote=True) + '">' + self.esc(text) + "</a>"

    def send(self, text, attempts=3):
        body = json.dumps(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        ).encode()
        for _ in range(attempts):
            if self._last is not None:
                gap = self.min_gap - (self.clock() - self._last)
                if gap > 0:
                    self.sleep(gap)
            self._last = self.clock()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with self.opener(req, timeout=self.timeout) as r:
                    return bool(json.loads(r.read()).get("ok"))
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    log.error("telegram HTTP %s: %s", e.code, _read(e)[:200])
                    return False
                try:
                    wait = json.loads(_read(e)).get("parameters", {}).get("retry_after", 5)
                except ValueError:
                    wait = int(e.headers.get("Retry-After") or 5) if e.headers else 5
                log.warning("telegram 429, waiting %ss", wait)
                self.sleep(min(float(wait), 120))
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.error("telegram unreachable: %s", e)
                return False
        return False


def _read(e):
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def from_env(env=None):
    """Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, else stdout."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return TelegramSink(token, chat)
    return ConsoleSink()


# ---- ordering ---------------------------------------------------------------

_RELATIVE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def age_days(posted_at, first_seen, now):
    """Days since posting; Workday's "Posted 3 Days Ago" understood; else first_seen."""
    s = (posted_at or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?", s)
    if m:
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
            return max(0.0, (now - ts) / 86400)
        except ValueError:
            pass
    m = _RELATIVE.search(s)
    if m:
        word = m.group(1).lower()
        return 0.0 if word == "today" else 1.0 if word == "yesterday" else float(m.group(2))
    return max(0.0, (now - (first_seen or now)) / 86400)


def order(alerts, now):
    """alerts carry a `score` tuple (filters.Filter.score); higher first, then newest."""
    return sorted(
        alerts,
        key=lambda a: (
            tuple(-x for x in a.get("score") or ()),
            age_days(a.get("posted_at"), a.get("first_seen"), now),
            -(a.get("first_seen") or 0),
        ),
    )


# ---- composition ------------------------------------------------------------

def pretty_date(posted_at):
    """ISO timestamps shown as plain dates; Workday's relative text left alone."""
    s = (posted_at or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


def render_entry(sink, a, with_company=False):
    title = sink.bold(a["title"])
    if with_company:
        title = sink.esc(a["company"]) + " · " + title
    meta = ["📍 " + sink.esc(a.get("location") or "?")]
    if a.get("posted_at"):
        meta.append("🗓 " + sink.esc(pretty_date(a["posted_at"])))
    if a.get("req_id"):
        meta.append("req " + sink.code(a["req_id"]))
    return f"{title}\n{' · '.join(meta)}\n{sink.link(a['url'], 'Apply')}"


def _pack(blocks, limit):
    """blocks: (text, company_header_or_None, keys). Greedy pack at block boundaries.

    Returns [(text, keys)]. A block whose header differs from the previous one
    starts with that header; a part that begins mid-company repeats it "(cont.)".
    """
    parts, cur, cur_keys, cur_len, cur_header = [], [], [], 0, None
    for text, header, keys in blocks:
        prefix = header + "\n" if header is not None and header != cur_header else ""
        piece = prefix + text
        added = len(piece) + (2 if cur else 0)
        if cur and cur_len + added > limit:
            parts.append(("\n\n".join(cur), cur_keys))
            cur, cur_keys, cur_len = [], [], 0
            if header is not None and not prefix:
                piece = header + " (cont.)\n" + text
            added = len(piece)
        if len(piece) > limit:
            piece, added = piece[: limit - 1] + "…", limit
        cur.append(piece)
        cur_keys.extend(keys)
        cur_len += added
        if header is not None:
            cur_header = header
    if cur:
        parts.append(("\n\n".join(cur), cur_keys))
    return parts


def _number(parts):
    if len(parts) == 1:
        return parts
    return [(f"({i}/{len(parts)})\n{t}", k) for i, (t, k) in enumerate(parts, 1)]


def _key(a):
    return (a["company"], a["job_id"])


def compose(sink, alerts, now=None, digest_threshold=DIGEST_THRESHOLD, top_n=DIGEST_TOP):
    """-> [(message text, [(company, job_id) delivered by it])]. Empty when nothing to say.

    A digest's keys ride on its last part: the digest only counts as delivered
    once all of it is.
    """
    if not alerts:
        return []
    now = now or time.time()
    ranked = order(alerts, now)
    limit = sink.max_len - 12          # room for the "(12/12)\n" part number

    if len(ranked) > digest_threshold:
        counts = {}
        for a in ranked:
            counts[a["company"]] = counts.get(a["company"], 0) + 1
        oldest = min(a.get("first_seen") or now for a in ranked)
        hours = max(1, math.ceil((now - oldest) / 3600) + 1)
        head = (
            f"🔔 {sink.bold(f'{len(ranked)} new matches')} across {len(counts)} companies\n"
            + "\n".join(f"• {sink.esc(c)}: {n}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        )
        blocks = [(head, None, []), ("Top " + str(min(top_n, len(ranked))) + ":", None, [])]
        blocks += [(render_entry(sink, a, with_company=True), None, []) for a in ranked[:top_n]]
        rest = len(ranked) - top_n
        tail = f"…and {rest} more: " + sink.code(f"watcher list --since {hours}h --matches")
        blocks.append((tail, None, [_key(a) for a in ranked]))
        return _number(_pack(blocks, limit))

    by_company, company_order = {}, []
    for a in ranked:
        if a["company"] not in by_company:
            by_company[a["company"]] = []
            company_order.append(a["company"])
        by_company[a["company"]].append(a)
    blocks = []
    for c in company_order:
        header = sink.bold(f"▍{c} ({len(by_company[c])} new)")
        blocks += [(render_entry(sink, a), header, [_key(a)]) for a in by_company[c]]
    return _number(_pack(blocks, limit))


def compose_backoff(sink, states, now=None):
    if not states:
        return []
    lines = [sink.bold("⚠️ job-watch: companies in extended backoff")]
    for st in states:
        until = time.strftime("%a %H:%M", time.localtime(st["blocked_until"] or now or time.time()))
        why = (st.get("last_error") or st.get("backoff_kind") or "").split(" from ")[0]
        lines.append(f"• {sink.esc(st['company'])}: {sink.esc(why)}, not polled until {until}")
    lines.append("No postings are checked for these until then; this is sent once per episode.")
    keys = [st["company"] for st in states]
    return _number(_pack([(l, None, []) for l in lines[:-1]] + [(lines[-1], None, keys)], sink.max_len - 12))
