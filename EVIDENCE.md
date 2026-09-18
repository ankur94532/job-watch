# Why 17 companies are not polled

Every company in `config.yaml` that the watcher does **not** poll, with the exact
request and response behind that decision. All pairs below were re-run on
**2026-09-18** with the watcher's real User-Agent:

```
job-watch/0.1.0 (personal job-alert poller)
```

Nothing here was written from memory. Four entries changed verdict when I re-ran
them for this table — see [What re-checking changed](#what-re-checking-changed).

The three `robots`-blocked boards were **not requested at all**. Only their
`robots.txt` was fetched, and the disallow line is quoted verbatim.

---

## Summary

| Reason | Count | Companies |
|---|---|---|
| `no-board` | 5 | chargebee, gupshup, krutrim, linkedin, qdrant |
| `dynamic-challenge` | 5 | blinkit, bookmyshow, meta, nutanix, uber |
| `robots` | 3 | flipkart, navi, vespa |
| `server-rendered` | 4 | medianet, nykaa, zepto, zomato |

`no-board` means there is no public list of openings to read — usually because
the careers page was removed or never listed jobs. It is not a statement about
difficulty.

---

## `robots` — a feed exists, and we are told not to read it

These three are the only cases where a working, parseable feed was found and
deliberately left alone. **The board URL was never requested.**

### flipkart

```
board (NOT requested)  https://flipkart.turbohire.co/careerpage/4d757ba0-3d57-448a-b82c-238ed87ac90f
GET                    https://flipkart.turbohire.co/robots.txt
->                     HTTP 200
matching rule          Disallow: /
verdict                allowed=False
```

TurboHire serves `Disallow: /` to every agent but Googlebot. The rule matches
our board URL, so nothing under that host is fetched.

### navi

```
board (NOT requested)  https://navi.turbohire.co/dashboardv2?orgId=3e818601-0baa-429c-b6f8-4b21903ae0e6
GET                    https://navi.turbohire.co/robots.txt
->                     HTTP 200
matching rule          Disallow: /
verdict                allowed=False
```

Same TurboHire policy on a different tenant.

> Not to be confused with the Ashby board under the token `navi`, which is a live
> San Francisco startup of the same name, not Navi Technologies. That near-miss
> is why config never accepts a guessed slug without `--accept-guess`.

### vespa

```
board (NOT requested)  https://vespaai.bamboohr.com/jobs/embed2.php?format=json
GET                    https://vespaai.bamboohr.com/robots.txt
->                     HTTP 200
matching rule          Disallow: /jobs/embed2.php
verdict                allowed=False
```

The single most annoying one: `embed2.php?format=json` returns exactly the clean
JSON this watcher wants, and BambooHR's `robots.txt` names that path specifically.
The rule is about the path, so no query-string variation gets around it.

> This is also the case that proves the matcher is worth having.
> `urllib.robotparser` drops query strings entirely, which silently changes what
> a rule means — see the docstring in [watcher/robots.py](watcher/robots.py).

---

## `dynamic-challenge` — needs a live browser on every fetch

A browser can load these; a plain HTTP client cannot, and the gap is a bot
challenge or a rotating per-session token, not a missing URL. Per the project
rule, Playwright is a discovery tool only and never runs under launchd, so a
source that needs a browser *per fetch* is out of scope by design.

### uber

```
GET   https://www.uber.com/us/en/careers/list/
->    HTTP 403, 5534 bytes, text/html; charset=UTF-8
      body matches /challenge-platform|cdn-cgi/challenge/
robots: allowed (no matching rule)
```

Blocked before any listing is served. Not a robots decision — they simply refuse
non-browser clients.

### nutanix

```
GET   https://careers.nutanix.com/en/jobs
->    HTTP 403, 5537 bytes, text/html; charset=UTF-8
      body matches /challenge-platform|cdn-cgi/challenge/
robots: allowed  (rule "Allow: /")
```

**Their robots.txt permits us** — this is a Cloudflare challenge, not a policy.
An earlier note here claimed `Disallow: /en/jobs/`; that rule exists but belongs
to the `SemrushBot` and `PageFreezer` groups, not to `*`. Misreading which group
a rule sits in is an easy way to invent a prohibition, so the verdict is recorded
as what it is: a challenge we choose not to defeat.

### bookmyshow

```
GET   https://in.bookmyshow.com/careers
->    HTTP 403, 5511 bytes, text/html; charset=UTF-8
      headings: "You are unable to access bookmyshow.com",
                "Why have I been blocked?",
                "What can I do to resolve this?"

watcher trace (real Chromium, 2026-09-18)
->    playwright TimeoutError: Page.goto timeout 45000ms exceeded
      navigating to "https://in.bookmyshow.com/careers", waiting until "networkidle"
```

The response says outright that we are blocked, and a real browser does no better
— the navigation never reaches `networkidle` and times out. So there is not even
a captured request to consider replaying.

### blinkit

```
GET   https://blinkit.com/careers/jobs
->    HTTP 403, 200000+ bytes, text/html; charset=UTF-8
browser trace: no JSON response carrying a job array
```

Gated to plain clients, and server-rendered even once a browser is through — so
there is no endpoint to lift out of the trace.

### meta

```
GET   https://www.metacareers.com/jobs
->    HTTP 200, 400000+ bytes, text/html; charset="utf-8"
      no __NEXT_DATA__, no __staticRouterHydrationData, no job titles in markup
browser trace: no JSON response carrying a job array was captured
```

The 200 is misleading: the shell loads and carries no listing, and the trace
captured nothing replayable. Meta's board is driven by GraphQL calls whose
`doc_id` is minted per build and paired with a per-session token, so even a
captured request is not a stable endpoint — it would work once and then fail
silently, which is worse than not polling at all.

> Honesty note: the "no job array captured" line above is today's trace output.
> The `doc_id`/session-token detail is carried over from the earlier
> investigation recorded in `config.yaml` and was **not** re-derived for this
> table — it is the explanation, not the evidence. The evidence is simply that
> two independent attempts produced no replayable request.

---

## `server-rendered` — readable, but only by parsing HTML

The listings exist in the served markup. The build spec rules out HTML scraping,
so these are recorded rather than polled. **These are the entries most likely to
flip if a company changes ATS** — each ships its jobs in HTML today, but a
migration to any real ATS would make it pollable immediately.

### zomato (Eternal)

```
GET   https://www.eternal.com/careers/
->    HTTP 200, 209919 bytes, text/html
      headings: "Our businesses", "About", "Get in touch"
browser trace: no JSON response carrying a job array
```

> Separately: the Greenhouse board `zomato1` exists but its newest requisition is
> from 2016, and the Lever board `eternal` is a US sports company. Both were
> rejected as false positives.

### zepto

```
GET   https://www.zeptonow.com/careers/jobs
->    HTTP 202, 2047 bytes, text/html; charset=UTF-8
browser trace (/careers and /careers/jobs): no JSON response carrying a job array
```

### medianet

```
GET   https://www.media.net/careers/
->    HTTP 200, 72554 bytes, text/html; charset=UTF-8
      headings: "We're not just a great place to work", "What Makes Us Awesome!",
                "Hiring Process"
browser trace: no JSON response carrying a job array
```

> The SmartRecruiters board `medianet` is a different company — a 2020 Tunisian
> board. Another rejected false positive.

### nykaa

```
GET   https://careers.nykaa.com/
->    HTTP 200, 12263 bytes, text/html
      Astro SSR; canonical https://careers.nykaa.com.skima.ai
      rendered body: "Currently there are no job postings available."
GET   https://skima-careers-frontend.pages.dev/_astro/client.BbzZVBOJ.js   -> 200, 1868 bytes
GET   https://skima-careers-frontend.pages.dev/_astro/InternalJobPostingLoginButton...js -> 200, 44311 bytes
      neither bundle contains an API host or /api/ path
```

Nykaa uses **Skima**. The job list is rendered into the HTML server-side, and
grepping both frontend bundles turned up no API base to call — so there is no
JSON endpoint hiding behind the page. Today the board is also genuinely empty.

---

## `no-board` — there is no public list of openings

### chargebee

```
GET   https://www.chargebee.com/careers/join-us/
->    HTTP 200, 309007 bytes, text/html
      headings: "Products", "Billing", "Payments", "Solutions", "Use cases"
      only outbound jobs link:
      https://www.linkedin.com/company/chargebee/jobs/
```

A product marketing page with an `#explore-apply` anchor whose one jobs link
points at LinkedIn. Chargebee runs no board of its own.

### gupshup

```
GET   https://www.gupshup.io/careers
->    HTTP 200, 104989 bytes, text/html (Next.js, __NEXT_DATA__ present)
      headings: "Your Next Career Move Is Just a Message Away",
                "There's always room for talent in every team"
GET   https://www.gupshup.ai/_next/data/edfaf3b2/en/careers.json?slug=careers
->    HTTP 200, 44999 bytes
      pageProps keys: config, agentconfigList, components, SFDCFormConfig, ...
      largest array = pageProps.agentconfigList (15 rows) -- their PRODUCT
      catalogue ("Religious Travel AI Agent (Hajj)"), not jobs
GET   https://gupshup.freshteam.com/jobs
->    HTTP 200, 889 bytes, <title>Freshteam</title>
      body: "We couldn't find ... You can claim it now" -- an UNCLAIMED subdomain
GET   https://gupshup.darwinbox.in/ms/candidate/careers        -> 200, 898 bytes
GET   https://gupshup.darwinbox.in/ms/candidate/careers/jobs   -> 200, 898 bytes
GET   https://gupshup.darwinbox.in/ms/candidate/careers/api/jobs -> 200, 898 bytes
      every path returns the identical 898-byte OG-preview stub (<title>Gupshup)
      with no app JS; a browser trace saw no XHR
```

Three dead ends, and two of them look alive if you only check the status code.
The Next.js route was previously recorded here as a job feed — **it is not**; the
array that scored as "job-shaped" was their product catalogue, which is exactly
the failure mode a title-plus-location heuristic has. The Freshteam subdomain is
unclaimed. The Darwinbox tenant is real but its candidate portal is switched off.
Gupshup's careers page asks for a message, not an application.

### qdrant

```
GET   https://qdrant.tech/careers                  -> HTTP 404, 25508 bytes
GET   https://qdrant.tech/jobs                     -> HTTP 404, 25508 bytes
GET   https://qdrant.tech/zzz-definitely-not-a-page -> HTTP 404, 25508 bytes
      identical body, <title>404 Page not found - Qdrant</title>
      homepage links to no careers page
```

Comparing against a deliberately nonsense path is what makes this conclusive:
identical byte counts mean `/careers` hits the generic 404 template, not a real
page. The careers page has been removed since it was last traced.

### krutrim

```
GET   https://www.olakrutrim.com/careers             -> HTTP 404, 7408 bytes
GET   https://www.olakrutrim.com/zzz-not-a-real-path -> HTTP 404, 7408 bytes
      identical body, <title>Krutrim Cloud</title>
GET   https://olakrutrim.com/careers                 -> 200 after redirect to the homepage
      homepage links: /ola-maps, /pricing, /support, /terms-and-conditions
```

Same test, same answer. The apex redirect to a 200 homepage is the trap here — it
looks like a working careers page in a status-code-only check.

### linkedin

```
GET   https://careers.linkedin.com/jobs   -> HTTP 404
GET   https://careers.linkedin.com/       -> HTTP 200, 149462 bytes
      every job link targets:
      https://www.linkedin.com/jobs/search/?f_C=1337,9202023,...&geoId=90009633
      (geoId 90009633 = Greater Bengaluru Area)
```

LinkedIn's careers site has no board of its own: it hands job seekers to
LinkedIn's own job search. That is excluded by the build spec, so LinkedIn is out
by rule, not by difficulty.

---

## What re-checking changed

Re-running these pairs for the table moved four entries. Three had been recorded
under a reason that was true when first checked and is not true now — which is
the argument for citing a dated request/response pair instead of a verdict.

| Company | Was | Now | What the re-check found |
|---|---|---|---|
| qdrant | `server-rendered` | `no-board` | The careers page was removed; `/careers` now returns the generic 404 body |
| krutrim | `server-rendered` | `no-board` | Same — `/careers` 404s with the same body as a nonsense path |
| linkedin | `server-rendered` | `no-board` | The traced URL now 404s; their real board is LinkedIn job search, excluded by spec |
| nykaa | `server-rendered` | `server-rendered` | Verdict held, but the reason is now specific: Skima SSR, no API host in the bundles, 0 postings today |

Because three of the four stale URLs returned 404 rather than an error, a
status-code check alone would have kept reporting them as "server-rendered"
indefinitely. Comparing each suspicious 404 against a deliberately nonsense path
on the same host is what settled them.

---

## How to re-run this

```sh
# robots decision for one URL, with the winning rule
python3 -c "from watcher.robots import allows; print(allows('<url>'))"

# what a board actually returns, without committing anything
python3 -m watcher test-adapter <company>

# discovery only, in the dev venv -- never runs under launchd
.venv-dev/bin/python -m watcher trace <company> --url <careers page>
```

A company should be re-checked when its careers page changes shape or when a
`404` shows up where a page used to be. Moving one out of this file means writing
a stdlib adapter and pasting a real, current title + location + URL from it —
the same bar every polled company had to clear.
