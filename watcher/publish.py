"""Publish the exported site: straight to Cloudflare Pages, or via a git branch.

`deploy: direct` uploads the files with
Cloudflare's own CLI, `wrangler pages deploy`. No build runs on Cloudflare, so
nothing queues: the Git-connected setup built on every push, one build at a
time, and a single build that hung at 20:44 froze the phone view for 40
minutes. It would also have used up the free plan's 500 builds a month within
a week, since every run changes the "checked" time.

`deploy: git` is the older path, kept for a git-connected Pages project:

Built with plumbing (`hash-object` / `update-index` / `commit-tree`) against a
temporary index, so the working tree and the current branch are never touched —
no checkout, no stash, nothing to clean up if the laptop sleeps mid-push.

The commit is parentless and the ref is replaced every time: one commit, always.
Without that, a JSON blob per run accumulates in the repo forever.

A push failure is never fatal. Waking with no network is the normal case.
"""

import logging
import os
import re
import subprocess
import tempfile

log = logging.getLogger(__name__)


class PublishError(Exception):
    pass


def git(args, repo, env=None, check=True):
    e = dict(os.environ, **(env or {}))
    p = subprocess.run(["git", "-C", repo] + args, capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise PublishError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout.strip()


def is_repo(repo):
    try:
        return git(["rev-parse", "--is-inside-work-tree"], repo) == "true"
    except (PublishError, FileNotFoundError):
        return False


def hash_path(blob_path, repo):
    return git(["hash-object", "-w", "--", blob_path], repo)


def publish(repo, out_dir, files, branch="site", remote="origin", message=None, push=True):
    """Commit `files` (paths under out_dir) to an orphan branch and force-push it."""
    if not is_repo(repo):
        raise PublishError(f"{repo} is not a git repository — see DEPLOY.md")
    rel = []
    for path in files:
        r = os.path.relpath(os.path.abspath(path), os.path.abspath(out_dir))
        if r.startswith("..") or os.path.isabs(r):
            raise PublishError(f"refusing to publish {path}: outside {out_dir}")
        if r.endswith((".db", ".db-wal", ".db-shm", ".lock")) or r == "config.yaml":
            raise PublishError(f"refusing to publish {r}")
        rel.append((r, path))

    with tempfile.NamedTemporaryFile(prefix="jobswatch-index-", delete=False) as tmp:
        index = tmp.name
    try:
        env = {"GIT_INDEX_FILE": index}
        git(["read-tree", "--empty"], repo, env)
        for r, path in sorted(rel):
            sha = hash_path(path, repo)
            git(["update-index", "--add", "--cacheinfo", f"100644,{sha},{r}"], repo, env)
        tree = git(["write-tree"], repo, env)
    finally:
        os.path.exists(index) and os.remove(index)

    msg = message or "site: update"
    commit = git(["commit-tree", tree, "-m", msg], repo)          # no -p: parentless, single commit
    git(["update-ref", f"refs/heads/{branch}", commit], repo)
    if push:
        git(["push", "--force", remote, f"{branch}:{branch}"], repo)
    return commit


DEFAULT_WRANGLER = "~/.local/share/jobswatch/wrangler/node_modules/.bin/wrangler"   # `make wrangler`
NODE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")   # launchd gives jobs no PATH; wrangler needs node


def publish_direct(out_dir, project, wrangler=None, branch="main", runner=None):
    """Upload `out_dir` to the Pages project `project`. Returns the deployment URL."""
    wrangler = os.path.expanduser(wrangler or DEFAULT_WRANGLER)
    if not os.access(wrangler, os.X_OK):
        raise PublishError(f"wrangler not found at {wrangler} (run `make wrangler`)")
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        raise PublishError("CLOUDFLARE_API_TOKEN is not set (deploy/jobswatch.env)")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*NODE_DIRS, env.get("PATH", "")])
    env.setdefault("WRANGLER_SEND_METRICS", "false")
    try:
        p = (runner or subprocess.run)([wrangler, "pages", "deploy", ".", "--project-name", project, "--branch", branch,
                    "--commit-dirty=true"], cwd=out_dir, capture_output=True, text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        raise PublishError("wrangler timed out after 300s") from None
    if p.returncode != 0:
        raise PublishError(f"wrangler: {(p.stderr or p.stdout).strip()[-300:]}")
    m = re.search(r"https://[\w.-]+\.pages\.dev", p.stdout or "")
    return m.group(0) if m else "deployed"


def publish_quietly(repo, out_dir, files, blob, state_path, deploy="git", project=None, wrangler=None, **kw):
    """Publish only when something published changed. Never raises; returns a status string.

    The digest covers every file, not just the job index: keyed on the index
    alone, a change to the page's own code (app.js, the sync function) sat
    unpublished until the next time the job list happened to change.
    """
    import hashlib

    h = hashlib.sha256(blob)
    for path in sorted(files):
        h.update(os.path.relpath(path, out_dir).encode() + b"\0")
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            pass                          # publish() will report the missing file
    digest = h.hexdigest()
    try:
        with open(state_path) as f:
            if f.read().strip() == digest:
                return "unchanged"
    except OSError:
        pass
    try:
        if deploy == "direct":
            result = publish_direct(out_dir, project, wrangler)
        else:
            result = publish(repo, out_dir, files, **kw)[:8]
    except (PublishError, OSError) as e:
        log.warning("site not published: %s", e)
        return f"failed: {e}"
    try:
        with open(state_path, "w") as f:
            f.write(digest + "\n")
    except OSError as e:
        log.warning("could not record export hash: %s", e)
    return f"{'deployed' if deploy == 'direct' else 'pushed'} {result}"
