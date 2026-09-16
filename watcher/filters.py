"""Title / location matching.

Permissive on purpose. Terms match on word boundaries after normalising
punctuation, so "Software Engineering - MTS" matches "mts" and
"Back-End" matches "back end", but "mts" does not match "Systems".

There is deliberately no seniority / years-of-experience filtering: Indian
offices post generic "Software Engineer" reqs with no level attached.
"""

import re


def normalize(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def _bounded(alt):
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


def compile_terms(terms):
    terms = [normalize(t) for t in terms or [] if normalize(t)]
    if not terms:
        return None
    return _bounded("|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True)))


class Filter:
    """An empty term list matches everything."""

    def __init__(self, title=None, location=None):
        self.title_terms = list(title or [])
        self.location_terms = list(location or [])
        self._title = compile_terms(self.title_terms)
        self._location = compile_terms(self.location_terms)
        self._title_each = [_bounded(re.escape(normalize(t))) for t in self.title_terms if normalize(t)]
        self._place = compile_terms([t for t in self.location_terms if normalize(t) != "remote"])

    def title_ok(self, title):
        return self._title is None or bool(self._title.search(normalize(title)))

    def location_ok(self, location):
        return self._location is None or bool(self._location.search(normalize(location)))

    def matches(self, posting):
        return self.title_ok(posting.title) and self.location_ok(posting.location)

    def score(self, title, location):
        """Sort key, higher = more relevant.

        (named place beats remote-only, earlier title term in the config list
        beats later ones, more distinct terms matched breaks ties).
        """
        t = normalize(title)
        hits = [i for i, rx in enumerate(self._title_each) if rx.search(t)]
        title_rank = (len(self._title_each) - hits[0]) if hits else 0
        place = 1 if self._place is not None and self._place.search(normalize(location)) else 0
        return (place, title_rank, len(hits))


def filter_for(company_cfg, defaults):
    """Per-company `title` / `location` lists replace the defaults wholesale."""
    return Filter(
        title=company_cfg.get("title", defaults.get("title")),
        location=company_cfg.get("location", defaults.get("location")),
    )
