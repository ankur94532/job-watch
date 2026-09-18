import argparse
import datetime as dt
import logging
import os
import re
import sys
import time

from . import config as config_mod
from . import export as export_mod
from . import notify, publish as publish_mod, runner
from .adapters import build_adapter
from .detect import Detector
from .filters import filter_for
from .lock import AlreadyRunning, RunLock
from .store import Store, db_path

log = logging.getLogger("watcher")


def parse_since(s):
    m = re.fullmatch(r"(\d+)\s*([mhdw])", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 30m, 12h, 7d, 2w")
    n, unit = int(m.group(1)), m.group(2)
    return int(time.time()) - n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def print_summary(res, seed):
    out = sys.stderr
    if res.offline:
        return
    for name, fetched, new, matched in res.ok:
        extra = "" if seed else f", {matched} new matches"
        print(f"  ok          {name:<22} {fetched:>5} open, {new} new{extra}", file=out)
    for name in res.unchanged:
        print(f"  unchanged   {name:<22} 304", file=out)
    for name, fetched, new in res.partial:
        print(f"  partial     {name:<22} {fetched} fetched, {new} new — sweep resumes next run", file=out)
    for name, err in res.failed:
        print(f"  FAILED      {name:<22} {err}", file=out)
    for name, until, err in res.backed_off:
        print(f"  BACKED-OFF  {name:<22} until {fmt_ts(until)} ({err.split(' from ')[0]})", file=out)
    if res.not_due:
        print(f"  not due     {len(res.not_due)}: " + ", ".join(n for n, _ in res.not_due), file=out)
    if res.deferred:
        print(f"  deferred    {len(res.deferred)} (out of time, next run): " + ", ".join(res.deferred), file=out)
    if res.skipped:
        print(f"  unsupported {len(res.skipped)}: " + ", ".join(n for n, _ in res.skipped), file=out)
    for name, age, interval, kind in getattr(res, "starving", []):
        what = "not polled" if kind == "poll" else "no complete sweep"
        print(f"  STARVING    {name:<22} {what} in {age / 3600:.1f}h "
              f"({age / interval:.0f}x its interval)", file=out)
    if getattr(res, "exported", None):
        print(f"  site        {res.exported}", file=out)
    if seed:
        print("seeded; no alerts sent", file=out)
    else:
        print(f"alerts: {res.sent} delivered" + (f", {res.unsent} pending retry" if res.unsent else ""), file=out)


def cmd_run(args, seed=False):
    cfg = config_mod.load(args.config)
    try:
        with RunLock(args.db + ".lock"):
            t0 = time.monotonic()
            store = Store(args.db)
            try:
                if getattr(args, "refetch", False):
                    n = store.clear_cache(set(args.company) if args.company else None)
                    log.info("dropped stored validators for %d compan%s",
                             n, "y" if n == 1 else "ies")
                res = runner.run(
                    cfg,
                    store,
                    sink=None if seed else notify.from_env(),
                    seed=seed,
                    only=set(args.company) if args.company else None,
                )
            finally:
                store.close()
            if not res.offline:
                log.info("%s finished in %.1fs", "seed" if seed else "run", time.monotonic() - t0)
    except AlreadyRunning as e:
        log.info("%s — exiting", e)
        return 0
    print_summary(res, seed)
    return res.exit_code


def cmd_list(args):
    cfg = config_mod.load(args.config)
    store = Store(args.db)
    rows = store.rows(company=args.company, since=args.since, closed=args.closed)
    store.close()
    defaults = cfg.get("defaults") or {}
    filters = {}
    shown = 0
    now = int(time.time())
    for r in rows:
        if args.matches:
            f = filters.setdefault(r["company"], filter_for(cfg["companies"].get(r["company"]) or {}, defaults))
            if not (f.title_ok(r["title"]) and f.location_ok(r["location"])):
                continue
        shown += 1
        req = f" [{r['req_id']}]" if r["req_id"] and r["req_id"] != r["job_id"] else ""
        if args.closed:
            head = (f"{fmt_ts(r['first_seen'])} → last seen {fmt_ts(r['last_seen'])} "
                    f"(open ~{fmt_age(r['last_seen'] - r['first_seen'])})")
        else:
            head = f"{fmt_ts(r['first_seen'])}  ({fmt_age(now - r['first_seen'])} ago)"
        print(f"{head}  {r['company']}  {r['job_id']}{req}\n  {r['title']}\n  {r['location']}\n  {r['url']}\n")
    print(f"{shown} rows", file=sys.stderr)
    return 0


def cmd_status(args):
    cfg = config_mod.load(args.config)
    store = Store(args.db)
    defaults = cfg["defaults"]
    now = int(time.time())
    active, skipped = config_mod.active_companies(cfg)
    print(f"db: {args.db}\n")
    print(f"{'company':<22} {'ats':<16} {'every':>6}  {'last poll':<16} {'next due':<16} state")
    for name, c in active:
        st = store.state(name)
        interval = config_mod.interval_for(c, defaults)
        due_at = (st["last_polled"] or 0) + interval - config_mod.DUE_SLACK
        if st["blocked_until"] and st["blocked_until"] > now:
            state = f"BACKED OFF until {fmt_ts(st['blocked_until'])}: {st['last_error']}"
        elif st["last_polled"] and now - st["last_polled"] > interval * runner.STARVING_POLLS:
            state = (f"STARVING: not polled in {(now - st['last_polled']) / 3600:.1f}h "
                     f"({(now - st['last_polled']) / interval:.0f}x interval)")
        elif st["cursor"]:
            pos = next((st["cursor"][k] for k in ("offset", "start") if st["cursor"].get(k) is not None), "?")
            state = f"sweep in progress (at {pos})"
        elif st["last_complete"] is None:
            state = "never seeded"
        elif now - st["last_complete"] > runner.sweep_threshold(c, defaults):
            # Polled fine, but the sweep never finishes, so closed reqs pile up.
            state = (f"STARVING: no complete sweep in "
                     f"{(now - st['last_complete']) / 3600:.1f}h, {st['last_count']} open")
        elif st["zero_runs"]:
            # HTML adapters: zero rows is never "fine". The run log says whether
            # the page confirmed it was empty or the selector matched nothing.
            state = f"0 rows for {st['zero_runs']} poll(s) in a row — see the log for why"
        elif st["last_error"]:
            state = f"last error: {st['last_error']}"
        else:
            state = f"ok, {st['last_count']} open" + (", etag" if st["cache"] else "")
        nxt = "now" if due_at <= now or st["cursor"] else fmt_ts(due_at)
        print(f"{name:<22} {c['ats']:<16} {fmt_age(interval):>6}  {fmt_ts(st['last_polled']):<16} {nxt:<16} {state}")
    store.close()
    if skipped:
        print(f"\nunsupported / disabled ({len(skipped)}): " + ", ".join(n for n, _ in skipped))
    return 0


def cmd_test_adapter(args):
    cfg = config_mod.load(args.config)
    c = cfg["companies"].get(args.company)
    if c is None:
        print(f"{args.company} is not in {args.config}", file=sys.stderr)
        return 2
    if not c.get("ats") or c.get("ats") == "unsupported":
        print(f"{args.company}: ats={c.get('ats')!r} — {c.get('note', 'nothing to fetch')}", file=sys.stderr)
        return 2
    adapter = build_adapter(args.company, c, page_delay=float(c.get("page_delay") or 0.5))
    flt = filter_for(c, cfg["defaults"])
    t0 = time.time()
    try:
        postings = adapter.fetch().postings
    except Exception as e:                      # a board being down is an answer, not a crash
        print(f"{args.company} ({c['ats']}): {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    took = time.time() - t0
    matches = [p for p in postings if flt.matches(p)]
    vague = [p for p in postings if flt.title_ok(p.title) and adapter.needs_enrich(p)]
    print(f"{args.company} ({c['ats']}): {len(postings)} postings, {adapter.http.requests} requests, "
          f"{took:.1f}s; {len(matches)} match filters")
    if vague:
        print(f"  +{len(vague)} title matches with a vague location ({vague[0].location!r}); `run` resolves them")
    print("\nsample rows:")
    for p in postings[:3]:
        print(f"  {p.job_id}  req={p.req_id or '-'}  {p.title}\n    {p.location} | {p.posted_at} | remote={p.remote}\n    {p.url}")
    if matches:
        print("\nsample matches:")
        for p in matches[:3]:
            print(f"  {p.job_id}  req={p.req_id or '-'}  {p.title}\n    {p.location} | {p.posted_at}\n    {p.url}")
    print("\n(nothing written)")
    return 0


def cmd_export(args):
    cfg = config_mod.load(args.config)
    settings = config_mod.export_settings(cfg)
    out = os.path.abspath(os.path.expanduser(args.out or settings["out"]))
    store = Store(args.db)
    try:
        files, blob = export_mod.write(cfg, store, out)
    finally:
        store.close()
    print(f"{len(files)} files, {len(blob)} bytes of JSON -> {out}", file=sys.stderr)
    if args.publish:
        repo = os.path.abspath(settings.get("repo") or os.path.dirname(out) or ".")
        status = publish_mod.publish_quietly(
            repo, out, files, blob, os.path.join(os.path.dirname(args.db), "export.sha256"),
            branch=settings["branch"], remote=settings["remote"],
            message=f"site: update {time.strftime('%Y-%m-%d %H:%M')}")
        print(f"publish: {status}", file=sys.stderr)
    return 0


def append_config(path, det):
    with open(path) as f:
        original = f.read()
    cfg = config_mod.load(path)
    if cfg["companies"].get(det.key):
        raise SystemExit(f"{det.key} already configured in {path}; not overwriting")
    text = re.sub(rf"(?m)^  {re.escape(det.key)}:\s*(\{{\}})?\s*$\n?", "", original)
    text = text.rstrip("\n") + "\n\n" + det.yaml_block() + "\n"
    with open(path, "w") as f:
        f.write(text)
    try:
        ok = config_mod.load(path)["companies"].get(det.key, {}).get("ats") == det.ats
    except Exception:
        ok = False
    if not ok:
        with open(path, "w") as f:
            f.write(original)
        raise SystemExit(f"appending to {path} did not parse back (is `companies:` the last block?); restored")


def cmd_recheck(args):
    """Has a board appeared for a company that had none? Monthly; never enables anything."""
    from . import recheck
    from .http import HttpClient

    cfg = config_mod.load(args.config)
    store = Store(args.db)
    try:
        lines = recheck.run(cfg, store, HttpClient(), sink=None if args.quiet else notify.from_env())
    finally:
        store.close()
    for line in lines:
        print(line)
    return 0


def cmd_trace(args):
    """Dev-only: drives a real browser to find the XHR a careers SPA calls."""
    from .trace import trace              # imported here so the poller never loads playwright
    cfg = config_mod.load(args.config)
    url = args.url
    if not url:
        c = cfg["companies"].get(args.company) or {}
        url = c.get("careers_url")
        if not url:
            print(f"no --url given and no careers_url in config for {args.company}", file=sys.stderr)
            return 2
    return trace(url, args.company, interact=not args.no_interact)


def cmd_detect(args):
    det = Detector(log=lambda m: print(m, file=sys.stderr)).detect(args.query)
    if det.samples:
        print("\nsample postings:", file=sys.stderr)
        for p in det.samples:
            print(f"  {p.title} | {p.location} | {p.posted_at}", file=sys.stderr)
    print()
    print(det.yaml_block())
    if args.write:
        if det.guessed and not args.accept_guess:
            print("\nnot written: this board was found by guessing the slug. "
                  "Check the samples, then re-run with --write --accept-guess.", file=sys.stderr)
            return 1
        append_config(args.config, det)
        print(f"\nappended {det.key} to {args.config}", file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="watcher", description="Watch ATS job boards for new postings.")
    ap.add_argument("--config", default=config_mod.config_path(), help="default: $JOBSWATCH_CONFIG or ./config.yaml")
    ap.add_argument("--db", default=db_path(), help="default: $JOBSWATCH_DB or ~/.local/share/jobswatch/jobs.db")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    for name, helptext in (("seed", "fetch every board, record it, alert nothing (do this first)"),
                           ("run", "one sweep of due companies; alert on new matches")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--company", action="append", help="limit to this company (repeatable)")
        p.add_argument("--refetch", action="store_true",
                       help="drop stored ETags first, so boards answer 200 instead of 304 "
                            "(use after changing an adapter: a 304 re-derives nothing)")

    p = sub.add_parser("list", help="show stored postings")
    p.add_argument("--company")
    p.add_argument("--closed", action="store_true", help="reqs that disappeared from their board")
    p.add_argument("--since", type=parse_since, help="first seen within e.g. 7d, 12h")
    p.add_argument("--matches", action="store_true", help="only rows matching current filters")

    sub.add_parser("status", help="per-company poll state: due times, backoff, sweeps in progress")

    p = sub.add_parser("export", help="render open matching reqs to a static site (no server)")
    p.add_argument("--out", help="output directory (default: export.out in config, else docs/)")
    p.add_argument("--publish", action="store_true", help="force-push it to the site branch")

    p = sub.add_parser("test-adapter", help="one fetch, print counts and samples, write nothing")
    p.add_argument("company")

    p = sub.add_parser("recheck", help="monthly: has a board appeared for a company with none? (not polling)")
    p.add_argument("--quiet", action="store_true", help="report only; send no message")

    p = sub.add_parser("trace", help="DEV ONLY: drive a browser to find a careers SPA's XHR endpoint")
    p.add_argument("company")
    p.add_argument("--url", help="careers page to load (default: careers_url from config)")
    p.add_argument("--no-interact", action="store_true", help="do not click pagination or scroll")

    p = sub.add_parser("detect", help="resolve a company's ATS and print a config block")
    p.add_argument("query", help="company name, domain, careers URL, or a job posting URL")
    p.add_argument("--write", action="store_true", help="append the block to config.yaml")
    p.add_argument("--accept-guess", action="store_true", help="allow --write for slug-guess detections")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    handlers = {
        "seed": lambda a: cmd_run(a, seed=True),
        "run": cmd_run,
        "list": cmd_list,
        "status": cmd_status,
        "export": cmd_export,
        "test-adapter": cmd_test_adapter,
        "detect": cmd_detect,
        "trace": cmd_trace,
        "recheck": cmd_recheck,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
