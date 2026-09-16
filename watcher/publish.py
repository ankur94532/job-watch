"""Push the exported site to an orphan `site` branch, one commit, force.

Built with plumbing (`hash-object` / `update-index` / `commit-tree`) against a
temporary index, so the working tree and the current branch are never touched —
no checkout, no stash, nothing to clean up if the laptop sleeps mid-push.

The commit is parentless and the ref is replaced every time: one commit, always.
Without that, a JSON blob per run accumulates in the repo forever.

A push failure is never fatal. Waking with no network is the normal case.
"""

import logging
import os
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


def publish_quietly(repo, out_dir, files, blob, state_path, **kw):
    """Publish only when the payload changed. Never raises; returns a status string."""
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    try:
        with open(state_path) as f:
            if f.read().strip() == digest:
                return "unchanged"
    except OSError:
        pass
    try:
        commit = publish(repo, out_dir, files, **kw)
    except (PublishError, OSError) as e:
        log.warning("site not published: %s", e)
        return f"failed: {e}"
    try:
        with open(state_path, "w") as f:
            f.write(digest + "\n")
    except OSError as e:
        log.warning("could not record export hash: %s", e)
    return f"pushed {commit[:8]}"
