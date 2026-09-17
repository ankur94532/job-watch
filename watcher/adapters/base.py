import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..http import HttpClient, jitter


@dataclass
class Posting:
    company: str
    ats: str
    job_id: str          # unique per posting on the board — the dedup key
    title: str
    location: str        # all locations, joined with " | "
    remote: bool
    posted_at: str       # as the ATS reports it (ISO date, or Workday's "Posted Today")
    url: str
    raw: dict = field(repr=False, default_factory=dict)
    req_id: str = ""     # requisition ID to quote in a referral request, when the ATS exposes one


@dataclass
class FetchContext:
    """Per-company state the runner persists between processes."""

    cache: dict = field(default_factory=dict)     # HTTP validators (ETag / Last-Modified)
    cursor: Optional[dict] = None                 # resume point of an unfinished sweep
    deadline: Optional[float] = None              # time.monotonic() value to stop paging at


@dataclass
class FetchResult:
    postings: List[Posting]
    complete: bool = True          # False: sweep paused, resume from `cursor`
    cursor: Optional[dict] = None
    # Set when the board throttled us mid-sweep. The rows already fetched are
    # still good: the runner records them, keeps the cursor, and backs off. The
    # alternative — raising — throws away work that cost requests to get, which
    # matters on a board that only allows a handful of requests per window.
    throttled: Optional[Exception] = None


class AdapterError(Exception):
    pass


class Adapter(ABC):
    ats = ""
    required = ()          # config keys this adapter needs
    paginated = False      # multi-request sweeps are scheduled after single-request boards

    def __init__(self, company, cfg, http=None, page_delay=0.5):
        missing = [k for k in self.required if not cfg.get(k)]
        if missing:
            raise AdapterError(f"{company}: {self.ats} config missing {', '.join(missing)}")
        self.company = company
        self.cfg = cfg
        self.http = http or HttpClient()
        self.page_delay = page_delay

    @abstractmethod
    def fetch(self, ctx=None):
        """Return a FetchResult.

        A complete result must hold every open posting: the store treats anything
        missing from a complete sweep as a closed requisition. Raise rather than
        return a short list. Raise http.NotModified when the board is unchanged.
        """

    def needs_enrich(self, posting):
        """True if the listing is too thin to filter on and a detail fetch would help."""
        return False

    def enrich(self, posting):
        return posting

    def partial_on_throttle(self, err, postings, cursor):
        """Give the runner what we already have, plus the error that stopped us."""
        return FetchResult(postings, complete=False, cursor=cursor, throttled=err)

    def pause(self):
        if self.page_delay:
            time.sleep(jitter(self.page_delay))


def as_list(value, default=None):
    """Config values that mean "one or more": a bare string is ONE item.

    list("bengaluru") is ["b","e","n",...], which silently turns a filter or a
    site list into nonsense that still runs. Every adapter takes its list-shaped
    config through here.
    """
    if value is None or value == "":
        return list(default or [])
    return [value] if isinstance(value, str) else list(value)


def join_locations(*parts):
    out = []
    for p in parts:
        items = p if isinstance(p, (list, tuple)) else [p]
        for s in items:
            s = (s or "").strip()
            if s and s not in out:
                out.append(s)
    return " | ".join(out)
