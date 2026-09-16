#!/usr/bin/env python3
"""Render the launchd plist template with real paths and the env file's secrets.

    python3 deploy/render_plist.py <template> <env-file> <output> [python] [workdir]

The env file is `KEY=value` lines (`#` comments allowed). It is gitignored and
its values end up only in the rendered plist, which is written mode 600.
"""

import os
import sys
import xml.sax.saxutils as sax

DEFAULT_ENV = {
    "PYTHONUNBUFFERED": "1",
    "LANG": "en_US.UTF-8",
}


def read_env(path):
    env = dict(DEFAULT_ENV)
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SystemExit(f"{path}:{n}: expected KEY=value, got {line!r}")
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main(argv):
    if len(argv) < 4:
        raise SystemExit(__doc__)
    template, env_file, out = argv[1], argv[2], argv[3]
    python = argv[4] if len(argv) > 4 else sys.executable
    workdir = os.path.abspath(argv[5] if len(argv) > 5 else ".")
    env = read_env(env_file)
    # Don't rely on WorkingDirectory for module resolution: launchd may not be able
    # to chdir into the repo (TCC-protected locations such as ~/Downloads), and then
    # `python3 -m watcher` fails with "No module named watcher".
    env.setdefault("PYTHONPATH", workdir)
    env.setdefault("JOBSWATCH_CONFIG", os.path.join(workdir, "config.yaml"))
    env.setdefault("JOBSWATCH_DB", os.path.expanduser("~/.local/share/jobswatch/jobs.db"))
    logdir = os.path.expanduser(env.get("JOBSWATCH_LOGDIR", "~/Library/Logs/jobswatch"))
    label = os.path.splitext(os.path.basename(out))[0]

    if "shims" in python or not os.path.isabs(python):
        raise SystemExit(f"interpreter must be an absolute, non-shim path (got {python})")
    if not os.access(python, os.X_OK):
        raise SystemExit(f"{python} is not executable")
    if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
        print(f"warning: {env_file} has no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; "
              f"alerts will go to the log instead", file=sys.stderr)
    if not env.get("JOBSWATCH_CONTACT_URL"):
        print(f"warning: no JOBSWATCH_CONTACT_URL in {env_file}; the User-Agent "
              f"will not name a repo to contact", file=sys.stderr)

    entries = "\n".join(
        f"    <key>{sax.escape(k)}</key>\n    <string>{sax.escape(v)}</string>"
        for k, v in sorted(env.items())
    )
    with open(template) as f:
        text = f.read()
    for placeholder, value in (
        ("@@LABEL@@", label),
        ("@@PYTHON@@", python),
        ("@@WORKDIR@@", workdir),
        ("@@LOGDIR@@", logdir),
        ("@@ENVIRONMENT@@", entries),
    ):
        text = text.replace(placeholder, sax.escape(value) if placeholder != "@@ENVIRONMENT@@" else value)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    os.makedirs(logdir, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    print(f"wrote {out} (mode 600), logs in {logdir}")


if __name__ == "__main__":
    main(sys.argv)
