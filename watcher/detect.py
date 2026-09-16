"""Resolve which ATS a company uses, verify it live, emit a config block.

Order (see README):
  0. The input is itself an ATS URL (the only way in for Workday).
  1. Fetch likely careers pages, follow redirects, inspect the final host.
  2. Grep the served HTML / inline JSON for ATS hostnames.
  3. Probe the keyless APIs with slug guesses. Weak evidence: slugs are
     first-come on these platforms (Lever `eternal` is a US sports company,
     SmartRecruiters `Zomato1` is a 2016 board), so guesses are flagged.
  4. Workday cannot be guessed; ask for a posting URL.
  5. Nothing: `ats: unsupported` with what was seen.
"""

import datetime as dt
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from .adapters import build_adapter
from .adapters.workday import WorkdayAdapter
from .http import HttpClient, user_agent

STALE_DAYS = 180
TOKEN = r"([A-Za-z0-9][A-Za-z0-9_.-]*)"

PATTERNS = [
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[a-z0-9-]+/([A-Za-z0-9_-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + TOKEN, re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?for=" + TOKEN, re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/" + TOKEN, re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/" + TOKEN, re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/" + TOKEN, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + TOKEN, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + TOKEN, re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + TOKEN, re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/" + TOKEN, re.I)),
]
BAD_TOKENS = {"embed", "v1", "js", "wday", "static", "assets", "api", "jobs", "login", "favicon.ico"}

# Seen in the wild but no adapter — reported in the `unsupported` note.
FINGERPRINTS = {
    "icims": r"\.icims\.com",
    "eightfold": r"eightfold\.ai",
    "avature": r"avature\.net|\bAvature\b",
    "radancy/talentbrew": r"talentbrew\.com",
    "successfactors": r"successfactors\.(?:com|eu)",
    "taleo": r"taleo\.net",
    "oracle recruiting cloud": r"oraclecloud\.com/hcmUI",
    "rippling ats": r"ats\.rippling\.com",
    "phenom": r"phenompeople\.com|cdn\.phenompeople",
    "jobvite": r"jobvite\.com",
    "workable": r"apply\.workable\.com",
    "bamboohr": r"\.bamboohr\.com",
    "darwinbox": r"darwinbox\.in",
    "keka": r"\.keka\.com",
    "zoho recruit": r"zohorecruit\.",
    "teamtailor": r"teamtailor\.com",
    "recruitee": r"recruitee\.com",
    "personio": r"jobs\.personio\.",
    "cloudflare challenge (page not readable)": r"<title>Just a moment\.\.\.</title>",
}


@dataclass
class Candidate:
    ats: str
    cfg: dict
    evidence: str
    guessed: bool = False


@dataclass
class Detection:
    key: str
    ats: str
    cfg: dict
    evidence: str = ""
    verified: str = ""
    guessed: bool = False
    warnings: list = field(default_factory=list)
    samples: list = field(default_factory=list)

    def yaml_block(self):
        lines = [f"  {self.key}:", f"    ats: {self.ats}"]
        for k, v in self.cfg.items():
            if isinstance(v, list):
                lines.append(f"    {k}: [{', '.join(v)}]")
            else:
                lines.append(f"    {k}: {_yaml_scalar(v)}")
        if self.evidence:
            lines.append(f"    # evidence: {self.evidence}")
        if self.verified:
            lines.append(f"    # verified: {self.verified}")
        for w in self.warnings:
            lines.append(f"    # WARNING: {w}")
        return "\n".join(lines)


def _yaml_scalar(v):
    s = str(v)
    return s if re.fullmatch(r"[A-Za-z0-9_.-]+", s) else '"' + s.replace('"', "'") + '"'


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_query(q):
    """-> (config key, domain or None, url or None)."""
    q = q.strip()
    # Any dotted, space-free token is treated as a domain — TLDs are not a fixed list
    # (qdrant.tech, hasura.io, media.net) and guessing one wrongly mangles the key.
    if "://" in q or ("." in q and not re.search(r"\s", q)):
        url = q if "://" in q else "https://" + q
        host = urllib.parse.urlparse(url).hostname or ""
        labels = [l for l in host.split(".") if l not in ("www", "careers", "jobs")]
        key = labels[0] if labels else slugify(q)
        is_ats = any(p.search(url) for _, p in PATTERNS)
        if is_ats:
            m = next(p.search(url) for _, p in PATTERNS if p.search(url))
            key = m.group(1).lower()
        return slugify(key), (None if is_ats else host), url
    return slugify(q), None, None


def find_candidates(text, evidence):
    found = Counter()
    for ats, pat in PATTERNS:
        for m in pat.finditer(text):
            if ats == "workday":
                tenant, shard, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
                if site.lower() in BAD_TOKENS:
                    continue
                found[(ats, tenant, shard, site)] += 1
            else:
                tok = m.group(1).rstrip(".")
                if tok.lower() in BAD_TOKENS:
                    continue
                found[(ats, tok)] += 1
    out = []
    workday = {}
    for key, _n in found.most_common():
        if key[0] == "workday":
            _, tenant, shard, site = key
            workday.setdefault((tenant, shard), []).append(site)
        elif not any(c.ats == key[0] and c.cfg["token"].lower() == key[1].lower() for c in out):
            out.append(Candidate(key[0], {"token": key[1]}, evidence))
    for (tenant, shard), sites in workday.items():
        out.insert(0, Candidate("workday", {"tenant": tenant, "shard": shard, "sites": sites}, evidence))
    return out


def fingerprints(text):
    return [name for name, pat in FINGERPRINTS.items() if re.search(pat, text, re.I)]


class Detector:
    def __init__(self, http=None, fetch_page=None, log=print, today=None):
        self.http = http or HttpClient(retries=1, timeout=20)
        self.fetch_page = fetch_page or self._fetch_page
        self.log = log
        self.today = today or dt.date.today()

    @staticmethod
    def _fetch_page(url):
        # Honest UA, same as the poller. Pages that refuse it are reported, not worked around.
        req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "text/html,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.geturl(), r.read(4_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.geturl() or url, e.read(4_000_000).decode("utf-8", "replace")

    def careers_urls(self, key, domain):
        domains = [domain] if domain else [f"{key}.com"]
        urls = []
        for d in domains:
            bare = re.sub(r"^(?:www|careers|jobs)\.", "", d)
            urls += [
                f"https://www.{bare}/careers",
                f"https://careers.{bare}/",
                f"https://jobs.{bare}/",
                f"https://www.{bare}/company/careers",
            ]
        return urls

    def detect(self, query):
        key, domain, url = parse_query(query)
        seen_fp = set()

        # 0. The query is an ATS URL.
        if url and not domain:
            cands = find_candidates(url, f"from URL {url}")
            det = self._verify_first(key, cands)
            if det:
                return det

        # 1 + 2. Careers pages: redirect target, then grep the body.
        pages = [url] if (url and domain) else []
        for u in pages + self.careers_urls(key, domain):
            try:
                status, final, body = self.fetch_page(u)
            except Exception as e:
                self.log(f"  {u}: {e.__class__.__name__}: {e}")
                continue
            self.log(f"  {u} -> {status} {final}")
            cands = find_candidates(final, f"careers page redirects to {final}")
            cands += find_candidates(body, f"ATS link in HTML of {final}")
            seen_fp.update(fingerprints(final + "\n" + body))
            det = self._verify_first(key, cands)
            if det:
                return det

        # 3. Slug probes.
        det = self._verify_first(key, self.probe_candidates(key))
        if det:
            return det

        # 4 + 5.
        note = "no Greenhouse/Lever/Ashby/SmartRecruiters/Workday board found"
        if seen_fp:
            note += "; careers site shows: " + ", ".join(sorted(seen_fp))
        det = Detection(key, "unsupported", {"note": note})
        det.warnings.append(
            f"if it is on Workday, search '{key} myworkdayjobs.com' for a live job link and "
            f"run: watcher detect <that URL>"
        )
        return det

    def probe_candidates(self, key):
        variants = list(dict.fromkeys([key, key.lower(), key.capitalize()]))
        out = []
        for ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            for v in variants if ats == "smartrecruiters" else variants[:1]:
                out.append(Candidate(ats, {"token": v}, f"slug guess '{v}' answered", guessed=True))
        return out

    def _verify_first(self, key, cands):
        """First verified candidate; a stale guessed board only if nothing better."""
        stale = None
        for c in cands:
            det = self.verify(key, c)
            if det is None:
                continue
            if det.guessed and any("stale" in w for w in det.warnings):
                stale = stale or det
                continue
            return det
        return stale

    def verify(self, key, cand):
        cfg = dict(cand.cfg)
        try:
            if cand.ats == "workday":
                good = []
                a = WorkdayAdapter(key, cfg, http=self.http, page_delay=0)
                total, postings = 0, []
                for site in cfg["sites"]:
                    try:
                        page = a.fetch_page(site, 0)
                    except Exception:
                        continue
                    rows = page.get("jobPostings") or []
                    if rows:
                        good.append(site)
                        total += page.get("total") or len(rows)
                        postings += [a._posting(site, r) for r in rows]
                if not good:
                    self.log(f"  workday {cfg['tenant']}.{cfg['shard']} sites {cfg['sites']}: no postings")
                    return None
                cfg["sites"] = good
                count = total
            else:
                postings = build_adapter(key, dict(cfg, ats=cand.ats), http=self.http, page_delay=0.5).fetch().postings
                count = len(postings)
        except Exception as e:
            self.log(f"  {cand.ats} {cfg}: {e.__class__.__name__}: {e}")
            return None
        if not postings:
            self.log(f"  {cand.ats} {cfg}: answered but board is empty")
            return None

        det = Detection(
            key,
            cand.ats,
            cfg,
            evidence=cand.evidence,
            verified=f"{self.today.isoformat()} live fetch, {count} open postings",
            guessed=cand.guessed,
            samples=postings[:3],
        )
        newest = newest_date(postings)
        if cand.guessed:
            det.warnings.append("found by guessing the slug — check the sample rows really are this company")
            name = board_name(cand.ats, postings)
            if name:
                det.warnings.append(f"board calls itself {name!r}")
        if newest and (self.today - newest).days > STALE_DAYS:
            det.warnings.append(f"stale: newest posting is from {newest.isoformat()}")
        self.log(f"  {cand.ats} {cfg}: OK, {count} postings")
        return det


def newest_date(postings):
    best = None
    for p in postings:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.posted_at or "")
        if m:
            d = dt.date.fromisoformat(m.group(1))
            best = d if best is None or d > best else best
    return best


def board_name(ats, postings):
    raw = postings[0].raw if postings else {}
    if ats == "smartrecruiters":
        return (raw.get("company") or {}).get("name")
    if ats == "greenhouse":
        return raw.get("company_name")
    return None
