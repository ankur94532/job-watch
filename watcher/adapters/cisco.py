"""Cisco's official Workday board with dynamically derived India locations.

Cisco's Phenom careers UI publishes Workday apply URLs and a country facet, but
its relevance-ordered pagination reshuffles between requests. Workday exposes
only individual office facets. Derive the India office ids from their published
descriptors at the start of each sweep, then hold that exact set in the cursor.
"""

import re

from .base import AdapterError, FetchContext
from .embedded import RobotsAware
from .workday import WorkdayAdapter

SITE = "Cisco_Careers"
FACET = "locations"
FACET_ID = re.compile(r"[0-9a-f]{32}")
LEGACY_INDIA = re.compile(r"^IND(?:IA)?[-_ ].+", re.I)


class CiscoAdapter(WorkdayAdapter):
    ats = "cisco"
    required = ()
    check_robots = RobotsAware.check_robots

    def __init__(self, company, cfg, **kwargs):
        merged = dict(cfg, tenant="cisco", shard="wd5", sites=[SITE],
                      require_exact_count=True)
        super().__init__(company, merged, **kwargs)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        stored = (ctx.cursor or {}).get("dynamic_locations")
        if stored is not None:
            if (not isinstance(stored, list) or not stored
                    or len(stored) != len(set(stored))
                    or any(not isinstance(value, str) or not FACET_ID.fullmatch(value)
                           for value in stored)):
                raise AdapterError(f"{self.company}: invalid Cisco location cursor")
            location_ids = stored
        else:
            location_ids = self.india_location_ids()
        self.cfg["applied_facets"] = {FACET: location_ids}
        result = super().fetch(ctx)
        if result.cursor is not None:
            result.cursor["dynamic_locations"] = location_ids
        return result

    def india_location_ids(self):
        url = f"{self.api(SITE)}/jobs"
        self.check_robots(url)
        payload = self.http.post_json(url, {
            "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "",
        })
        facets = payload.get("facets") if isinstance(payload, dict) else None
        if not isinstance(facets, list):
            raise AdapterError(f"{self.company}: Workday response has no location facets")
        groups = [group for facet in facets if isinstance(facet, dict)
                  and facet.get("facetParameter") == "locationMainGroup"
                  for group in (facet.get("values") or [])
                  if isinstance(group, dict) and group.get("facetParameter") == FACET]
        if len(groups) != 1 or not isinstance(groups[0].get("values"), list):
            raise AdapterError(f"{self.company}: missing/ambiguous Workday location facet")
        selected = []
        descriptors = set()
        for value in groups[0]["values"]:
            if not isinstance(value, dict):
                raise AdapterError(f"{self.company}: invalid Workday location facet value")
            descriptor, location_id, count = (value.get(key) for key in
                                               ("descriptor", "id", "count"))
            india = (isinstance(descriptor, str)
                     and (descriptor == "India" or descriptor.endswith(", India")
                          or LEGACY_INDIA.fullmatch(descriptor)))
            if not india:
                continue
            if (not isinstance(location_id, str) or not FACET_ID.fullmatch(location_id)
                    or type(count) is not int
                    or count <= 0 or descriptor in descriptors or location_id in selected):
                raise AdapterError(f"{self.company}: invalid/duplicate India location facet")
            descriptors.add(descriptor)
            selected.append(location_id)
        if "Bangalore, India" not in descriptors or not selected:
            raise AdapterError(f"{self.company}: Cisco India anchor location is missing")
        return sorted(selected)
