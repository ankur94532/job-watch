PYTHON ?= /usr/bin/python3
LABEL  := com.shashwattripathi.jobswatch
REPO   := $(CURDIR)
PLIST_SRC := $(REPO)/deploy/$(LABEL).plist
PLIST     := $(HOME)/Library/LaunchAgents/$(LABEL).plist
RECHECK_LABEL := $(LABEL).recheck
RECHECK_SRC   := $(REPO)/deploy/$(RECHECK_LABEL).plist
RECHECK_PLIST := $(HOME)/Library/LaunchAgents/$(RECHECK_LABEL).plist
ENV_FILE  := $(REPO)/deploy/jobswatch.env
LOGDIR    := $(HOME)/Library/Logs/jobswatch
DB        ?= $(HOME)/.local/share/jobswatch/jobs.db
DOMAIN    := gui/$(shell id -u)
SITE_BRANCH := site

.PHONY: help test seed run status list install uninstall reinstall logs kick export site sitecheck
help:
	@echo "make test       run the test suite (no network)"
	@echo "make seed       first step: record every board, alert nothing"
	@echo "make run        one sweep now, in this terminal"
	@echo "make status     per-company due times, backoff, sweeps in progress"
	@echo "make export     render the phone view into docs/ (no publish)"
	@echo "make sitecheck  drive the exported page in a browser (needs .venv-dev)"
	@echo "make site       export and publish using the method in config.yaml"
	@echo "make install    render the plist and load the launchd agent (seed first)"
	@echo "make kick       force a run now via launchctl"
	@echo "make recheck    has a board appeared for a company with none? (monthly agent does this)"
	@echo "make logs       tail stdout and stderr logs"
	@echo "make uninstall  unload the agent (database and logs are kept)"

test:
	cd $(REPO) && $(PYTHON) -m unittest discover -s tests -t . -v
	@# the tick-sync Pages Function; skipped, loudly, where there is no node
	@cd $(REPO) && if command -v node >/dev/null; then node --test tests/site_state.test.mjs; else echo 'node not found: skipped tests/site_state.test.mjs'; fi

seed:
	cd $(REPO) && $(PYTHON) -m watcher seed

run:
	cd $(REPO) && $(PYTHON) -m watcher run

status:
	cd $(REPO) && $(PYTHON) -m watcher status

export:
	cd $(REPO) && $(PYTHON) -m watcher export

site:
	cd $(REPO) && $(PYTHON) -m watcher export --publish

$(ENV_FILE):
	@cp $(REPO)/deploy/jobswatch.env.example $(ENV_FILE)
	@echo "created $(ENV_FILE) — fill in the Telegram token and chat id, then re-run"
	@exit 1

install: $(ENV_FILE)
	@test -f "$(DB)" || { echo "no database at $(DB) — run 'make seed' first, or every open req alerts at once"; exit 1; }
	$(PYTHON) $(REPO)/deploy/render_plist.py $(PLIST_SRC) $(ENV_FILE) $(PLIST) $(PYTHON) $(REPO)
	-launchctl bootout $(DOMAIN)/$(LABEL) 2>/dev/null
	launchctl bootstrap $(DOMAIN) $(PLIST)
	launchctl enable $(DOMAIN)/$(LABEL)
	@echo
	@launchctl list | grep $(LABEL) || echo "not registered — check $(LOGDIR)/jobswatch.err.log"
	@$(MAKE) --no-print-directory install-recheck

# Monthly "has a board appeared?" check. Its own agent: never part of polling.
install-recheck: $(ENV_FILE)
	$(PYTHON) $(REPO)/deploy/render_plist.py $(RECHECK_SRC) $(ENV_FILE) $(RECHECK_PLIST) $(PYTHON) $(REPO)
	-launchctl bootout $(DOMAIN)/$(RECHECK_LABEL) 2>/dev/null
	launchctl bootstrap $(DOMAIN) $(RECHECK_PLIST)
	launchctl enable $(DOMAIN)/$(RECHECK_LABEL)

recheck:
	cd $(REPO) && $(PYTHON) -m watcher recheck

# Cloudflare's CLI for `deploy: direct` (see watcher/publish.py). Pinned under the
# data dir, outside the repo, and called by absolute path: launchd has no PATH.
wrangler:
	mkdir -p $(HOME)/.local/share/jobswatch/wrangler
	cd $(HOME)/.local/share/jobswatch/wrangler && { test -f package.json || echo '{"private":true}' > package.json; } \
		&& npm install --no-fund --no-audit wrangler@4

uninstall:
	-launchctl bootout $(DOMAIN)/$(LABEL)
	-launchctl bootout $(DOMAIN)/$(RECHECK_LABEL)
	@rm -f $(PLIST) $(RECHECK_PLIST)
	@echo "agent removed; $(DB) and $(LOGDIR) kept"

reinstall: uninstall install

kick:
	launchctl kickstart -p $(DOMAIN)/$(LABEL)

logs:
	@mkdir -p $(LOGDIR)
	@touch $(LOGDIR)/jobswatch.log $(LOGDIR)/jobswatch.err.log
	tail -f $(LOGDIR)/jobswatch.log $(LOGDIR)/jobswatch.err.log

sitecheck: export           ## drive the exported page in a real browser (needs .venv-dev)
	.venv-dev/bin/python devtools/site_check.py
