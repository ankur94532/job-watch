import os

from . import yamlish
from .adapters import ADAPTERS, UNSUPPORTED

# Deliberate policy decision, not an unfinished gap: `blocked: robots` means the
# site's own robots.txt forbids us, `blocked: dynamic-challenge` means it needs a
# live browser on every fetch.
BLOCKED = "blocked"

DEFAULT_INTERVAL = 900          # launchd fires every 15 minutes
PAGED_INTERVAL = 3600           # paginated boards (Workday, Eightfold, amazon.jobs):
                                # dozens of requests per sweep and mostly no ETag
DUE_SLACK = 60                  # launchd's 900s can land a few seconds early


def config_path(env=None):
    env = os.environ if env is None else env
    return os.path.expanduser(env.get("JOBSWATCH_CONFIG") or "config.yaml")


def load(path):
    with open(path) as f:
        cfg = yamlish.loads(f.read()) or {}
    cfg["defaults"] = cfg.get("defaults") or {}
    cfg["companies"] = cfg.get("companies") or {}
    for name, c in list(cfg["companies"].items()):
        cfg["companies"][name] = c or {}
    return cfg


def active_companies(cfg, only=None):
    """(name, cfg) for companies with a runnable adapter, plus (name, note) skipped."""
    active, skipped = [], []
    for name, c in cfg["companies"].items():
        if only and name not in only:
            continue
        ats = c.get("ats")
        if not ats or ats in (UNSUPPORTED, BLOCKED) or c.get("enabled") is False:
            why = c.get("note") or ("disabled" if c.get("enabled") is False else "no ats configured")
            if ats == BLOCKED:
                why = f"[{c.get('blocked') or 'blocked'}] {why}"
            skipped.append((name, why))
        elif ats not in ADAPTERS:
            skipped.append((name, f"unknown ats {ats!r}"))
        else:
            active.append((name, c))
    return active, skipped


def export_settings(cfg):
    """`export:` block, with the defaults that make a laptop + Pages setup work."""
    e = dict(cfg.get("export") or {})
    e.setdefault("enabled", False)
    e.setdefault("out", "docs")
    e.setdefault("publish", False)
    e.setdefault("branch", "site")
    e.setdefault("remote", "origin")
    e.setdefault("deploy", "git")          # "direct": wrangler upload, no Cloudflare build
    e.setdefault("data", "static")         # "kv": refresh jobs without a site deployment
    return e


def interval_for(company_cfg, defaults):
    """Per-company override, else hourly for boards that need many requests."""
    if company_cfg.get("interval"):
        return int(company_cfg["interval"])
    adapter = ADAPTERS.get(company_cfg.get("ats"))
    if adapter is not None and adapter.paginated:
        return int(defaults.get("paged_interval") or PAGED_INTERVAL)
    return int(defaults.get("interval") or DEFAULT_INTERVAL)
