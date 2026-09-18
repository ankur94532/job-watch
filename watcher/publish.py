"""Publish the exported site: straight to Cloudflare Pages, or via a git branch.

With `data: kv`, each successful poll writes one `site:snapshot` value. Changing
job data never deploys the website; `deploy: direct` uploads the HTML, scripts
and Functions with `wrangler pages deploy` only when those files change. This
keeps 15-minute data freshness without a queue or thousands of deployments.

`deploy: git` is the older path, kept for a git-connected Pages project:

Built with plumbing (`hash-object` / `update-index` / `commit-tree`) against a
temporary index, so the working tree and the current branch are never touched —
no checkout, no stash, nothing to clean up if the laptop sleeps mid-push.

The commit is parentless and the ref is replaced every time: one commit, always.
Without that, a JSON blob per run accumulates in the repo forever.

A push failure is never fatal. Waking with no network is the normal case.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

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


def publish_direct(out_dir, project, wrangler=None, branch="main", runner=None, files=None):
    """Upload `out_dir` to the Pages project `project`. Returns the deployment URL."""
    wrangler = os.path.expanduser(wrangler or DEFAULT_WRANGLER)
    if not os.access(wrangler, os.X_OK):
        raise PublishError(f"wrangler not found at {wrangler} (run `make wrangler`)")
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        raise PublishError("CLOUDFLARE_API_TOKEN is not set (deploy/jobswatch.env)")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*NODE_DIRS, env.get("PATH", "")])
    env.setdefault("WRANGLER_SEND_METRICS", "false")
    stage = None
    deploy_dir = out_dir
    try:
        if files is not None:
            stage = tempfile.TemporaryDirectory(prefix="jobswatch-site-")
            deploy_dir = stage.name
            for path in files:
                rel = os.path.relpath(os.path.abspath(path), os.path.abspath(out_dir))
                if rel.startswith("..") or os.path.isabs(rel):
                    raise PublishError(f"refusing to publish {path}: outside {out_dir}")
                dst = os.path.join(deploy_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(path, dst)
        p = (runner or subprocess.run)([wrangler, "pages", "deploy", ".", "--project-name", project, "--branch", branch,
                    "--commit-dirty=true"], cwd=deploy_dir, capture_output=True, text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        raise PublishError("wrangler timed out after 300s") from None
    finally:
        if stage is not None:
            stage.cleanup()
    if p.returncode != 0:
        raise PublishError(f"wrangler: {(p.stderr or p.stdout).strip()[-300:]}")
    m = re.search(r"https://[\w.-]+\.pages\.dev", p.stdout or "")
    return m.group(0) if m else "deployed"


SNAPSHOT_KEY = "site:snapshot"
KV_MAX_VALUE = 25 * 1024 * 1024


def publish_snapshot(blob, account, namespace, token=None, opener=None):
    """Write the current job snapshot to one KV key through Cloudflare's API."""
    token = token or os.environ.get("CLOUDFLARE_API_TOKEN")
    account = account or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token:
        raise PublishError("CLOUDFLARE_API_TOKEN is not set (deploy/jobswatch.env)")
    if not account:
        raise PublishError("CLOUDFLARE_ACCOUNT_ID is not set (deploy/jobswatch.env)")
    if not namespace:
        raise PublishError("export.kv_state is not set")
    if len(blob) > KV_MAX_VALUE:
        raise PublishError(f"job snapshot is {len(blob)} bytes; KV allows {KV_MAX_VALUE}")
    key = urllib.parse.quote(SNAPSHOT_KEY, safe="")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/"
           f"namespaces/{namespace}/values/{key}")
    req = urllib.request.Request(url, data=blob, method="PUT", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    try:
        with (opener or urllib.request.urlopen)(req, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[-300:]
        raise PublishError(f"Cloudflare KV HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PublishError(f"Cloudflare KV: {e}") from None
    if body:
        try:
            reply = json.loads(body)
        except (ValueError, TypeError):
            reply = None
        if isinstance(reply, dict) and reply.get("success") is False:
            raise PublishError(f"Cloudflare KV: {str(reply.get('errors') or reply)[-300:]}")
    return len(blob)


def publish_snapshot_quietly(blob, state_path, account, namespace, token=None, opener=None):
    """Update KV unless this exact snapshot already succeeded. Never raises."""
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    try:
        with open(state_path) as f:
            if f.read().strip() == digest:
                return "snapshot unchanged"
    except OSError:
        pass
    try:
        size = publish_snapshot(blob, account, namespace, token=token, opener=opener)
    except PublishError as e:
        log.warning("job snapshot not published: %s", e)
        return f"snapshot failed: {e}"
    try:
        with open(state_path, "w") as f:
            f.write(digest + "\n")
    except OSError as e:
        log.warning("could not record snapshot hash: %s", e)
    return f"snapshot updated ({(size + 1023) // 1024} KiB)"


def publish_export(repo, out_dir, files, index_blob, state_dir, settings, message=None):
    """Publish changing data and site code through their configured paths.

    With `data: kv`, every successful poll updates one KV value. Pages receives
    a deployment only when code/config files change; the volatile data files
    are deliberately excluded from both the code digest and deployment.
    """
    data_status = None
    deploy_files = files
    digest_blob = index_blob
    if settings.get("data") == "kv":
        from . import export as export_mod
        snapshot = export_mod.snapshot_blob(out_dir, index_blob)
        data_status = publish_snapshot_quietly(
            snapshot, os.path.join(state_dir, "snapshot.sha256"),
            settings.get("account") or os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
            settings.get("kv_state"),
        )
        if data_status.startswith("snapshot failed:"):
            return data_status
        deploy_files = [p for p in files
                        if not os.path.relpath(p, out_dir).startswith("data" + os.sep)]
        digest_blob = b""                 # changing job data must not deploy site code

    site_status = publish_quietly(
        repo, out_dir, deploy_files, digest_blob, os.path.join(state_dir, "export.sha256"),
        branch=settings["branch"], remote=settings["remote"],
        deploy=settings["deploy"], project=settings.get("project"), wrangler=settings.get("wrangler"),
        message=message,
    )
    return f"{data_status}; code {site_status}" if data_status else site_status


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
            result = publish_direct(out_dir, project, wrangler, files=files)
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
