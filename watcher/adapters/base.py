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
    # For adapters that can poll incrementally (newest-first boards): the ids
    # already stored, and when the last COMPLETE sweep started. Unused otherwise.
    known: Optional[set] = None
    last_complete: Optional[int] = None
    now: Optional[float] = None


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
    # HTML adapters only. `declared` is the count the page itself states
    # ("Showing 5 of 5", "4 Positions"), checked against the rows parsed.
    # `empty_evidence` is the page's own words when it says there are no
    # openings: the only thing that separates "genuinely zero" from "the
    # selector stopped matching", which otherwise look identical.
    declared: Optional[int] = None
    empty_evidence: Optional[str] = None
    # A poll that read only the newest pages and stopped at known ids: it can
    # add postings but says nothing about which ones closed, so it never
    # retires anything and never counts as a complete sweep.
    incremental: bool = False


class AdapterError(Exception):
    pass


class Adapter(ABC):
    ats = ""
    required = ()          # config keys this adapter needs
    paginated = False      # multi-request sweeps are scheduled after single-request boards
    max_parallel = None    # cap on this ATS's companies polled at once, even on different hosts
    # Parses a web page rather than a JSON API. A page whose markup changes
    # does not crash; it quietly yields zero rows, which reads exactly like a
    # quiet week. The runner treats zero rows from these as a warning state.
    html = False

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

    def lane(self):
        """Companies in the same lane are never polled at the same time.

        One lane per host: the pacing between requests is politeness towards a
        server, so it has to hold per server, but nothing is gained by making
        Workday wait on Greenhouse. Every multi-tenant API (all Greenhouse boards
        hit boards-api.greenhouse.io) shares one lane under the ATS name; an
        adapter configured with its own `host` gets a lane of its own.
        """
        host = self.cfg.get("host")
        return f"{self.ats}:{host}" if host else self.ats

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
